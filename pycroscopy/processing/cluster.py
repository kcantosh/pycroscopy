# -*- coding: utf-8 -*-
"""
Created on Tue Jan 05 07:55:56 2016

@author: Suhas Somnath, Chris Smith
"""
from __future__ import division, print_function, absolute_import
import numpy as np
import sklearn.cluster as cls
from scipy.cluster.hierarchy import linkage
from scipy.spatial.distance import pdist
from multiprocessing import cpu_count
from ..io.hdf_utils import getH5DsetRefs, checkAndLinkAncillary, copy_main_attributes, checkIfMain
from ..io.io_hdf5 import ioHDF5
from ..io.io_utils import check_dtype, transformToTargetType
from ..io.microdata import MicroDataGroup, MicroDataset


class Cluster(object):
    """
    Pycroscopy wrapper around the sklearn.cluster classes.
    """

    def __init__(self, h5_main, method_name, num_comps=None, *args, **kwargs):
        """
        Constructs the Cluster object

        Parameters
        ------------
        h5_main : HDF5 dataset object
            Main dataset with ancillary spectroscopic, position indices and values datasets
        method_name : string / unicode
            Name of the sklearn.cluster estimator
        num_comps : (optional) unsigned int
            Number of features / spectroscopic indices to be used to cluster the data. Default = all
        *args and **kwargs : arguments to be passed to the estimator
        """

        allowed_methods = ['AgglomerativeClustering', 'Birch', 'KMeans',
                           'MiniBatchKMeans', 'SpectralClustering']
        
        # check if h5_main is a valid object - is it a hub?
        if not checkIfMain(h5_main):
            raise TypeError('Supplied dataset is not a pycroscopy main dataset')

        if method_name not in allowed_methods:
            raise TypeError('Cannot work with {} just yet'.format(method_name))

        self.h5_main = h5_main

        '''
        If n_jobs is not provided, set to n_cores-2
        '''
        kwargs.update({'n_jobs': kwargs.pop('n_jobs', max(1, cpu_count() - 2))})

        # Instantiate the clustering object
        self.estimator = cls.__dict__[method_name].__call__(*args, **kwargs)
        self.method_name = method_name

        comp_slice, num_comps = self._get_component_slice(num_comps)

        self.num_comps = num_comps
        self.data_slice = (slice(None), comp_slice)

        # figure out the operation that needs need to be performed to convert to real scalar
        retval = check_dtype(h5_main)
        self.data_transform_func, self.data_is_complex, self.data_is_compound, \
        self.data_n_features, self.data_n_samples, self.data_type_mult = retval

    def do_cluster(self, rearrange_clusters=True):
        """
        Clusters the hdf5 dataset, calculates mean response for each cluster, and writes the labels and mean response
        back to the h5 file

        Parameters
        ----------
        rearrange_clusters : (Optional) Boolean. Default = True
            Whether or not the clusters should be re-ordered by relative distances between the mean response

        Returns
        --------
        h5_group : HDF5 Group reference
            Reference to the group that contains the clustering results
        """
        self._fit()
        new_mean_response = self._get_mean_response(self.results.labels_)
        new_labels = self.results.labels_
        if rearrange_clusters:
            new_labels, new_mean_response = reorder_clusters(self.results.labels_, new_mean_response)
        return self._write_to_hdf5(new_labels, new_mean_response)

    def _fit(self):
        """
        Fits the provided dataset

        Returns
        ------
        None
        """
        print('Performing clustering on {}.'.format(self.h5_main.name))
        # perform fit on the real dataset
        self.results = self.estimator.fit(self.data_transform_func(self.h5_main[self.data_slice]))

    def _get_mean_response(self, labels):
        """
        Gets the mean response for each cluster

        Parameters
        -------------
        labels : 1D unsigned int array
            Array of cluster labels as obtained from the fit

        Returns
        ---------
        mean_resp : 2D numpy array
            Array of the mean response for each cluster arranged as [cluster number, response]
        """
        print('Calculated the Mean Response of each cluster.')
        num_clusts = len(np.unique(labels))
        mean_resp = np.zeros(shape=(num_clusts, self.num_comps), dtype=self.h5_main.dtype)
        for clust_ind in range(num_clusts):
            # get all pixels with this label
            targ_pos = np.argwhere(labels == clust_ind)
            # slice to get the responses for all these pixels, ensure that it's 2d
            data_chunk = np.atleast_2d(self.h5_main[:, self.data_slice[1]][targ_pos, :])
            # transform to real from whatever type it was
            avg_data = np.mean(self.data_transform_func(data_chunk), axis=0, keepdims=True)
            # transform back to the source data type and insert into the mean response
            mean_resp[clust_ind] = transformToTargetType(avg_data, self.h5_main.dtype)
        return mean_resp

    def _get_component_slice(self, components):
        """
        Check the components object to determine how to use it to slice the dataset

        Parameters
        ----------
        components : {int, iterable of ints, slice, or None}
            Input Options
            integer: Components less than the input will be kept
            length 2 iterable of integers: Integers define start and stop of component slice to retain
            other iterable of integers or slice: Selection of component indices to retain
            None: All components will be used
        Returns
        -------
        comp_slice : slice or numpy array of uints
            Slice or array specifying which components should be kept
        """

        comp_slice = slice(None)

        if components is None:
            num_comps = self.h5_main.shape[1]
            comp_slice = slice(0, num_comps)
        elif isinstance(components, int):
            # Component is integer
            num_comps = int(np.min([components, self.h5_main.shape[1]]))
            comp_slice = slice(0, num_comps)
        elif hasattr(components, '__iter__') and not isinstance(components, dict):
            # Component is array, list, or tuple
            if len(components) == 2:
                # If only 2 numbers are given, use them as the start and stop of a slice
                comp_slice = slice(int(components[0]), int(components[1]))
                num_comps = abs(comp_slice.stop-comp_slice.start)
            else:
                #Convert components to an unsigned integer array
                comp_slice = np.uint(components)
                num_comps = len(comp_slice)
        elif isinstance(components, slice):
            # Components is already a slice
            comp_slice = components
            num_comps = abs(comp_slice.stop-comp_slice.start)
        elif components is not None:
            raise TypeError('Unsupported component type supplied to clean_and_build.  Allowed types are integer, numpy array, list, tuple, and slice.')

        return comp_slice, num_comps

    def _write_to_hdf5(self, labels, mean_response):
        """
        Writes the labels and mean response to the h5 file

        Parameters
        ------------
        labels : 1D unsigned int array
            Array of cluster labels as obtained from the fit
        mean_response : 2D numpy array
            Array of the mean response for each cluster arranged as [cluster number, response]

        Returns
        ---------
        h5_labels : HDF5 Group reference
            Reference to the group that contains the clustering results
        """
        print('Writing clustering results to file.')
        num_clusters = mean_response.shape[0]
        ds_label_mat = MicroDataset('Labels', np.uint32(labels.reshape([-1, 1])), dtype=np.uint32)
        clust_ind_mat = np.transpose(np.atleast_2d(np.arange(num_clusters)))

        ds_cluster_inds = MicroDataset('Cluster_Indices', np.uint32(clust_ind_mat))
        ds_cluster_vals = MicroDataset('Cluster_Values', np.float32(clust_ind_mat))
        ds_cluster_centroids = MicroDataset('Mean_Response', mean_response, dtype=mean_response.dtype)
        ds_label_inds = MicroDataset('Label_Spectroscopic_Indices', np.atleast_2d([0]), dtype=np.uint32)
        ds_label_vals = MicroDataset('Label_Spectroscopic_Values', np.atleast_2d([0]), dtype=np.float32)

        # write the labels and the mean response to h5
        clust_slices = {'Cluster': (slice(None), slice(0, 1))}
        ds_cluster_inds.attrs['labels'] = clust_slices
        ds_cluster_inds.attrs['units'] = ['']
        ds_cluster_vals.attrs['labels'] = clust_slices
        ds_cluster_vals.attrs['units'] = ['']

        cluster_grp = MicroDataGroup(self.h5_main.name.split('/')[-1] + '-Cluster_', self.h5_main.parent.name[1:])
        cluster_grp.addChildren([ds_label_mat, ds_cluster_centroids, ds_cluster_inds, ds_cluster_vals, ds_label_inds,
                                 ds_label_vals])

        cluster_grp.attrs['num_clusters'] = num_clusters
        cluster_grp.attrs['num_samples'] = self.h5_main.shape[0]
        cluster_grp.attrs['cluster_algorithm'] = self.method_name

        h5_spec_inds = self.h5_main.file[self.h5_main.attrs['Spectroscopic_Indices']]
        h5_spec_vals = self.h5_main.file[self.h5_main.attrs['Spectroscopic_Values']]

        '''
        Setup the Spectroscopic Indices and Values for the Mean Response if we didn't use all components
        '''
        if self.num_comps != self.h5_main.shape[1]:
            ds_centroid_indices = MicroDataset('Mean_Response_Indices', np.arange(self.num_comps, dtype=np.uint32))

            if isinstance(self.data_slice[1], np.ndarray):
                centroid_vals_mat = h5_spec_vals[self.data_slice[1].tolist()]
                cluster_grp.attrs['components_used'] = self.data_slice[1].tolist()

            else:
                centroid_vals_mat = h5_spec_vals[self.data_slice[1]]

                cluster_grp.attrs['components_used'] = range(self.data_slice[1].start,
                                                             self.data_slice[1].stop)[self.data_slice[1]]

            ds_centroid_values = MicroDataset('Mean_Response_Values', centroid_vals_mat)

            cluster_grp.addChildren([ds_centroid_indices, ds_centroid_values])

        else:
            cluster_grp.attrs['components_used'] = 'all'

        '''
        Get the parameters of the estimator used and write them
        as attributes of the group
        '''
        for parm in self.estimator.get_params().keys():
            cluster_grp.attrs[parm] = self.estimator.get_params()[parm]

        hdf = ioHDF5(self.h5_main.file)
        h5_clust_refs = hdf.writeData(cluster_grp)

        h5_labels = getH5DsetRefs(['Labels'], h5_clust_refs)[0]
        h5_centroids = getH5DsetRefs(['Mean_Response'], h5_clust_refs)[0]
        h5_clust_inds = getH5DsetRefs(['Cluster_Indices'], h5_clust_refs)[0]
        h5_clust_vals = getH5DsetRefs(['Cluster_Values'], h5_clust_refs)[0]
        h5_label_inds = getH5DsetRefs(['Label_Spectroscopic_Indices'], h5_clust_refs)[0]
        h5_label_vals = getH5DsetRefs(['Label_Spectroscopic_Values'], h5_clust_refs)[0]

        copy_main_attributes(self.h5_main, h5_centroids)

        if isinstance(self.data_slice[1], np.ndarray):
            h5_mean_resp_inds = getH5DsetRefs(['Mean_Response_Indices'], h5_clust_refs)[0]
            h5_mean_resp_vals = getH5DsetRefs(['Mean_Response_Values'], h5_clust_refs)[0]
        else:
            h5_mean_resp_inds = h5_spec_inds
            h5_mean_resp_vals = h5_spec_vals

        # h5_label_inds.attrs['labels'] = ''
        # h5_label_inds.attrs['units'] = ''
        # h5_label_vals.attrs['labels'] = ''
        # h5_label_vals.attrs['units'] = ''

        checkAndLinkAncillary(h5_labels,
                              ['Position_Indices', 'Position_Values'],
                              h5_main=self.h5_main)
        checkAndLinkAncillary(h5_labels,
                              ['Spectroscopic_Indices', 'Spectroscopic_Values'],
                              anc_refs=[h5_label_inds, h5_label_vals])

        checkAndLinkAncillary(h5_centroids,
                              ['Spectroscopic_Indices', 'Spectroscopic_Values'],
                              anc_refs=[h5_mean_resp_inds, h5_mean_resp_vals])

        checkAndLinkAncillary(h5_centroids,
                              ['Position_Indices', 'Position_Values'],
                              anc_refs=[h5_clust_inds, h5_clust_vals])

        # return the h5 group object
        return h5_labels.parent


def reorder_clusters(labels, mean_response):
    """
    Reorders clusters by the distances between the clusters

    Parameters
    ----------
    labels : 1D unsigned int numpy array
        Labels for the clusters
    mean_response : 2D numpy array
        Mean response of each cluster arranged as [cluster , features]

    Returns
    -------
    new_labels : 1D unsigned int numpy array
        Labels for the clusters arranged by distances
    new_mean_response : 2D numpy array
        Mean response of each cluster arranged as [cluster , features]
    """

    num_clusters = mean_response.shape[0]
    # Get the distance between cluster means
    distance_mat = pdist(mean_response)
    # get hierarchical pairings of clusters
    linkage_pairing = linkage(distance_mat, 'weighted')

    # get the new order - this has been checked to be OK
    new_cluster_order = []
    for row in range(linkage_pairing.shape[0]):
        for col in range(2):
            if linkage_pairing[row, col] < num_clusters:
                new_cluster_order.append(int(linkage_pairing[row, col]))

    # Now that we know the order, rearrange the clusters and labels:
    new_labels = np.zeros(shape=labels.shape, dtype=labels.dtype)
    new_mean_response = np.zeros(shape=mean_response.shape, dtype=mean_response.dtype)

    # Reorder clusters
    for old_clust_ind, new_clust_ind in enumerate(new_cluster_order):
        new_labels[np.where(labels == new_clust_ind)[0]] = old_clust_ind
        new_mean_response[old_clust_ind] = mean_response[new_clust_ind]

    return new_labels, new_mean_response

# -*- coding: utf-8 -*-
"""
Created on Sat Nov 07 15:21:46 2015

@author: Suhas Somnath
"""
from __future__ import division, print_function, absolute_import, unicode_literals

from os import path, listdir, remove
from warnings import warn

import numpy as np
from scipy.io.matlab import loadmat  # To load parameters stored in Matlab .mat file

from .df_utils.be_utils import parmsToDict
from .translator import Translator
from .utils import generate_dummy_main_parms, build_ind_val_dsets
from ..hdf_utils import getH5DsetRefs, linkRefs
from ..io_hdf5 import ioHDF5
from ..microdata import MicroDataGroup, MicroDataset


class GLineTranslator(Translator):
    """
    Translated G-mode line (bigtimedata.dat) files from actual BE line experiments to HDF5
    """
    
    def translate(self, file_path):
        """
        The main function that translates the provided file into a .h5 file
        
        Parameters
        ----------
        file_path : String / unicode
            Absolute path of any file in the directory

        Returns
        -------
        h5_path : String / unicode
            Absolute path of the h5 file
        """
        # Figure out the basename of the data:
        (basename, parm_paths, data_paths) = self._parse_file_path(file_path)
        
        (folder_path, unused) = path.split(file_path)
        h5_path = path.join(folder_path, basename+'.h5')
        
        if path.exists(h5_path):
            remove(h5_path)
        
        # Load parameters from .mat file - 'BE_wave', 'FFT_BE_wave', 'total_cols', 'total_rows'
        matread = loadmat(parm_paths['parm_mat'], variable_names=['BE_wave', 'FFT_BE_wave', 'total_cols', 'total_rows'])
        be_wave = np.float32(np.squeeze(matread['BE_wave']))

        # Need to take the complex conjugate if reading from a .mat file
        # FFT_BE_wave = np.conjugate(np.complex64(np.squeeze(matread['FFT_BE_wave'])))
        
        num_cols = int(matread['total_cols'][0][0])
        expected_rows = int(matread['total_rows'][0][0])
        self.points_per_pixel = len(be_wave)
        
        # Load parameters from .txt file - 'BE_center_frequency_[Hz]', 'IO rate'
        is_beps, parm_dict = parmsToDict(parm_paths['parm_txt'])
        
        # Get file byte size:
        # For now, assume that bigtime_00 always exists and is the main file
        file_size = path.getsize(data_paths[0])
        
        # Calculate actual number of lines since the first few lines may not be saved
        self.num_rows = 1.0 * file_size / (4 * self.points_per_pixel * num_cols)
        if self.num_rows % 1:
            warn('Error - File has incomplete rows')
            return None
        else:
            self.num_rows = int(self.num_rows)

        samp_rate = parm_dict['IO_rate_[Hz]']
        ex_freq_nominal = parm_dict['BE_center_frequency_[Hz]']

        # method 1 for calculating the correct excitation frequency:
        pixel_duration = 1.0 * self.points_per_pixel / samp_rate
        num_periods = pixel_duration * ex_freq_nominal
        ex_freq_correct = 1 / (pixel_duration / np.floor(num_periods))

        # method 2 for calculating the exact excitation frequency:
        """
        fft_ex_wfm = np.abs(np.fft.fftshift(np.fft.fft(be_wave)))
        w_vec = np.linspace(-0.5 * samp_rate, 0.5 * samp_rate - 1.0*samp_rate / self.points_per_pixel,
                            self.points_per_pixel)
        hot_bins = np.squeeze(np.argwhere(fft_ex_wfm > 1E+3))
        ex_freq_correct = w_vec[hot_bins[-1]]
        """

        # correcting the excitation frequency - will be VERY useful during analysis and filtering
        parm_dict['BE_center_frequency_[Hz]'] = ex_freq_correct

        # Some very basic information that can help the processing crew
        parm_dict['num_bins'] = self.points_per_pixel
        parm_dict['grid_num_rows'] = self.num_rows
        parm_dict['data_type'] = 'G_mode_line'
            
        if self.num_rows != expected_rows:
            print('Note: {} of {} lines found in data file'.format(self.num_rows, expected_rows))
        
        # Calculate number of points to read per line:
        self.__bytes_per_row__ = int(file_size/self.num_rows)

        # First finish writing all global parameters, create the file too:
        meas_grp = MicroDataGroup('Measurement_000')
        meas_grp.attrs = parm_dict

        spm_data = MicroDataGroup('')
        global_parms = generate_dummy_main_parms()
        global_parms['data_type'] = 'G_mode_line'
        global_parms['translator'] = 'G_mode_line'
        spm_data.attrs = global_parms
        spm_data.addChildren([meas_grp])
        
        hdf = ioHDF5(h5_path)
        # hdf.clear()
        hdf.writeData(spm_data)
        
        # Now that the file has been created, go over each raw data file:
        # 1. write all ancillary data. Link data. 2. Write main data sequentially
                
        """ We only allocate the space for the main data here.
        This does NOT change with each file. The data written to it does.
        The auxiliary datasets will not change with each raw data file since
        only one excitation waveform is used"""
        ds_main_data = MicroDataset('Raw_Data', data=[], 
                                    maxshape=(self.num_rows, self.points_per_pixel * num_cols),
                                    chunking=(1, self.points_per_pixel), dtype=np.float16)
        ds_main_data.attrs['quantity'] = ['Deflection']
        ds_main_data.attrs['units'] = ['V']

        ds_pos_ind, ds_pos_val = build_ind_val_dsets([self.num_rows], is_spectral=False,
                                                     labels=['Y'], units=['m'])
        ds_spec_inds, ds_spec_vals = build_ind_val_dsets([self.points_per_pixel * num_cols], is_spectral=True,
                                                         labels=['Excitation'], units=['V'])
        ds_spec_vals.data = np.atleast_2d(np.tile(np.float32(be_wave), num_cols))  # Override the default waveform
        
        aux_ds_names = ['Position_Indices', 'Position_Values',
                        'Spectroscopic_Indices', 'Spectroscopic_Values']
        
        for f_index in data_paths.keys():
            
            chan_grp = MicroDataGroup('{:s}{:03d}'.format('Channel_', f_index), '/Measurement_000/')
            chan_grp.addChildren([ds_main_data, ds_pos_ind, ds_pos_val, ds_spec_inds, ds_spec_vals])
            
            # print('Writing following tree to file:')
            # chan_grp.showTree()
            h5_refs = hdf.writeData(chan_grp)
            
            h5_main = getH5DsetRefs(['Raw_Data'], h5_refs)[0]  # We know there is exactly one main data
            
            # Reference linking can certainly take place even before the datasets have reached their final size         
            linkRefs(h5_main, getH5DsetRefs(aux_ds_names, h5_refs))
            
            # Now transfer scan data in the dat file to the h5 file:
            self._read_data(data_paths[f_index], h5_main)
            
        hdf.close()
        print('G-Line translation complete!')

        return h5_path

    @staticmethod
    def _parse_file_path(data_filepath):
        """
        Goes through the file directory and figures out the basename and the 
        parameter (text and .mat), data file paths (for each analog input channel)
        
        Parameters
        -----------------
        data_filepath : string / unicode
            absolute path of any file in the data folder
        
        Returns
        ----------------
        basename : string / unicode
            base name of the experiment\n
        parm_paths : dictionary
            paths for the text and .mat parameter files\n
            parm_text : absolute file path of the parameter text file\n
            parm_mat : absolute file path of the parameter .mat file
        data_paths : dictionary of the paths for the big-time data files.
            key : index of the analog input that generated the data file\n
            value : absolute file path of the data file
        """
        # Return (basename, parameter text path)
        (folder_path, basename) = path.split(data_filepath)
        (upper_folder, basename) = path.split(folder_path)
        
        # There may be one or two bigdata files. May need both paths
        parm_paths = dict()
        data_paths = dict()
        targ_str = 'bigtime_0'
        for filenames in listdir(folder_path):
            ind = filenames.find(targ_str)
            if ind > 0 and filenames.endswith('.dat'):
                data_paths[int(filenames[ind+len(targ_str)])] = path.join(folder_path, filenames)
        
            if filenames.endswith('.txt') and filenames.find('parm') > 0:
                parm_paths['parm_txt'] = path.join(folder_path, filenames)
                
            if filenames.endswith('_all.mat'):
                parm_paths['parm_mat'] = path.join(folder_path, filenames)
                
        return basename, parm_paths, data_paths

    def _read_data(self, filepath, h5_dset):
        """
        Reads the .dat file and populates the .h5 dataset

        Parameters
        ---------
        filepath : String / unicode
            absolute path of the data file for a particular analog input channel
        h5_dset : HDF5 dataset reference
            Reference to the target Raw_Data dataset

        Returns
        ---------
        None
        """
        # Create data matrix - Only need 16 bit floats (time) 
                
        # Read line by line and write to h5                 
        with open(filepath, 'rb') as file_handl:
            for row_indx in range(self.num_rows):
                
                if row_indx % 10 == 0:
                    print('Reading line {} of {}'.format(row_indx, self.num_rows))
                
                file_handl.seek(row_indx*self.__bytes_per_row__, 0)
                data_vec = np.fromstring(file_handl.read(self.__bytes_per_row__), dtype='f')
                h5_dset[row_indx] = np.float16(data_vec)
                h5_dset.file.flush()
        
        print('Finished reading file: {}!'.format(filepath))

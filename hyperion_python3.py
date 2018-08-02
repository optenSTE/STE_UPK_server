#
# hyperion.py
#
# Copyright (c) 2014 by Micron Optics, Inc.  All Rights Reserved
#
"""Module for interfacing to a Micron Optics, Inc. Hyperion instrument

Release Notes:

Version 1.1.0.0
Added All functions in the Sensors API

Version 1.0.0.0

Added functions for the new peak detection settings interface.

The interface to get_spectrum has changed significantly, to match changes in
firmware.  There are now commands for setting and getting the active full 
spectrum channels.  It is advisable to use these commands whenever the user
is only interested in getting a subset of all of the channels, as it instructs
the firmware to only process the data for the selected channels, thus freeing
up processor bandwidth for other tasks.

Running get_spectrum() with no arguments will acquire the data for ALL of the 
active channels.  Running get spectrum with a single channel specified will
return data for only that one channel, but only if that channel is also one of
the active channels.  It is no longer possible to supply a list of channels to
get_spectrum().

Added functions for all commands in the HOST section of firmware.

Version 0.9.5.0

This version will only work on new systems that use 16 bit spectra, with
numerous changes that are detailed in the latest system documentation.

Error handling has been substantially changed, to be more consistent with
python conventions.  Exceptions of type HyperionError are no longer handled
within the Hyperion class.  These exceptions need to be handled in the application.

Peak detection presets functionality has been removed.  In order to create 
and apply presets, the UI Example executable must be used.

Version 0.9.4.4

Changed preset parameters to 32 bit.

Version 0.9.4.3

More robust with systems that have fewer than 16 channels.

Version 0.9.4.2

Fixed a bug that affected the way that calibration data is loaded.  If 
loading the calibration fails on initialization, then calibrated spectrum
output is disabled.

Version 0.9.4.1

Added peak detection parameter presets capability.

Version 0.9.4.0

Calibrated Output
If numpy and scipy packages are installed, then the spectrum will be output
in calibrated dBm units.  If the raw spectrum is desired, CAL_OUTPUT_ENABLED
can be set to False.

HACQSpectrum class
The HACQSpectrum class is no longer derived from the array class.  Instead,
HACQSpectrum objects have a 'data' instance variable, which is an array 
containing the spectrum values.  If calibrated output is enabled (automatic if
numpy and scipy are installed), then 'data' is a numpy array.
"""
import socket
from array import array
from struct import *
import queue
from multiprocessing import Process, Queue as MPQueue
import os

USE_NUMPY = True
EXTRAP_EXTENT = 10
ZERO_OFFSET = 1000.0

try:
    import numpy as np
except ImportError:
    USE_NUMPY = False


from collections import namedtuple
# Library version is set to be the same as for the Labview Version
_LIBRARY_VERSION = '1.1.0.0 py3'

DEFAULT_TIMEOUT = 10000
HEADER_LENGTH = 8

SUCCESS = 0

PRESETS_ENABLED = True

RECV_BUFFER_SIZE = 4096
MAX_STREAMING_ERRORS = 10

COMMAND_PORT = 51971
STREAM_PEAKS_PORT = 51972
STREAM_SPECTRA_PORT = 51973
STREAM_SENSORS_PORT = 51974


class Hyperion:
    """This class defines the API to Hyperion instruments.
    
    Instance variables:
    
    comm - This is a communications object that implements the HComm interface.
           As of this version, the only type of interface is TCP, implemented 
           in a HCommTCPSocket object.
    
    peaks - A HACQPeaks object that contains all the peaks from the last 
            call to get_peaks or stream_peaks
    
    peaksHeader - A HACQPeaksHeader object associated with the most recent
                  peaks data.
    
    spectrum - A HACQSpectrum object that contains the most recent spectrum
               acquired using get_spectrum.
    
    spectrumHeader - A HACQSpectrumHeader object associated with the most
                     recent spectrum acquired.
    """

    # ******************************************************************************
    # Initialization
    # ******************************************************************************
    def __init__(self, ipAddress=None, commandPort=COMMAND_PORT,
                 timeout=DEFAULT_TIMEOUT, comm=None):
        """
        Keyword Arguments:

        ipAddress -- string containing the ipV4 address of the hyperion
                     instrument, e.g. '10.0.0.199'
        commandPort -- TCP port that is used for sending and receiving data.
                       The default is hyperion.COMMAND_PORT
        timeout -- Timeout value for TCP operations, in milliseconds.  The
                   The default is hyperion.DEFAULT_TIMEOUT
        comm -- A communications object derived from HComm
        """

        if comm == None:
            self.comm = HCommTCPSocket(ipAddress, commandPort, timeout)
        else:
            self.comm = comm

        self.spectrumStreamComm = None
        self.peakStreamComm = None
        serialNumber = self.get_serial_number()
        self._presetsModified = False
        self.dutOffset = 4
        # Get constants from instrument that will improve speed of full
        # spectrum calculations
        self.offset, self.scale = self.get_power_cal_offset_scale()

        self.invScale = []

        for scaleVal in self.scale:
            self.invScale.append(1.0 / scaleVal)

        self.numChannels = self.get_channel_count()
        self.wavelengthStart = self.get_wavelength_start()
        self.wavelengthNumPoints = self.get_wavelength_number_of_points()
        self.wavelengthDelta = self.get_wavelength_delta()

        self.wavelengths = []
        for wavelengthStepIndex in range(self.wavelengthNumPoints):
            self.wavelengths.append(self.wavelengthStart + wavelengthStepIndex * self.wavelengthDelta)

        self.scanSpeed = self.get_laser_scan_speed()
        # Use activeChannelBits to keep track of the bitfield in the spectrum header
        self.activeChannelBits = 0

    # ******************************************************************************
    # SYSTEM API
    # ******************************************************************************

    def get_serial_number(self):
        """Return the serial number from the Hyperion instrument.

        """
        serialNum = self.comm.execute_command('#GetSerialNumber', '', 0)
        serialNum['content'] = serialNum['content'].decode('utf-8')
        return serialNum

    def get_library_version(self):

        """Return the version of this API library"""
        return _LIBRARY_VERSION

    def get_version(self):
        """Return the FW, FPGA, and Library Versions"""
        versions = dict()

        versions['Firmware'] = \
            self.comm.execute_command('#GetFirmwareVersion')['content'].decode('utf-8')
        versions['Fpga'] = \
            self.comm.execute_command('#GetFpgaVersion')['content'].decode('utf-8')
        versions['hLibrary'] = _LIBRARY_VERSION

        return versions

    def get_instrument_name(self):
        """Returns the instrument name"""
        return self.comm.execute_command('#GetInstrumentName')['content'].decode('utf-8')

    def set_instrument_name(self, instrumentName):
        """Set the instrument name

        :param instrumentName: String containing the new instrument name
        :return: None
        """

        self.comm.execute_command('#SetInstrumentName', instrumentName)

    def is_ready(self):
        """Returns true if the system is actively scanning and ready to return data"""

        return unpack('B', self.comm.execute_command('#isready')['content'])[0] > 0


    def reboot(self):
        """Reboots the system after a 2 second delay"""
        self.comm.execute_command('#Reboot')

    def _get_user_data(self, slot):

        userData = self.comm.execute_command('#GetUserData', str(slot))

        return userData['content']

    def _set_user_data(self, slot, data):

        data = chr(slot) + data
        result = self.comm.execute_command('#SetUserData', data)
        return result

    # ******************************************************************************
    # DETECTION API
    # ******************************************************************************

    def get_channel_count(self):
        """Get the number of available channels on the instrument
        
        Return value:
        Integer value of the number of available channels.
        """
        channelCountBytes = self.comm.execute_command('#GetDutChannelCount')['content']
        dutChannelCount = unpack('I', channelCountBytes)[0]

        return dutChannelCount

    def get_max_peak_count_per_channel(self):
        """Get the maximum number of peaks that can be detected on each channel
        
        Return Value:
        Integer value of the maximum number of peaks.
        """
        peakCountBytes = self.comm.execute_command('#GetMaximumPeakCountPerDutChannel', '', 0)['content']
        maxPeakCountPerChannel = unpack('I', peakCountBytes)[0]

        return maxPeakCountPerChannel

    def get_detection_setting(self, detectionSettingID):
        """Get the detection settings corresponding to the provided detectionSettingID

        Keyword argument:

        detectionSettingID -- The ID of the detection setting.  Must be a value
                              between 0 and 255, inclusive.  If it does not match
                              an existing ID, an error is returned.

        Return Value:

        A HPeakDetectionSettings instance.

        """

        argument = "{0}".format(detectionSettingID)
        detectionSettingsData = self.comm.execute_command("#getDetectionSetting", argument)['content']
        detectionSettings, remainingData = HPeakDetectionSettings.from_binary_data(detectionSettingsData)
        return detectionSettings

    def get_available_detection_settings(self):
        """Get all available detection settings presets that are present on the instrument.
        
        Return Value:

        A list of HPeakDetectionSettings instances
        """

        detectionSettingsData = self.comm.execute_command("#getAvailableDetectionSettings")['content']
        allDetectionSettings = []
        while len(detectionSettingsData):
            detectionSettings, detectionSettingsData = HPeakDetectionSettings.from_binary_data(detectionSettingsData)
            allDetectionSettings.append(detectionSettings)

        return allDetectionSettings

    def add_detection_setting(self, detectionSetting):
        """Add a new detection setting.
        If the setting ID is already in use, an error will be returned.
        
        Keyword Arguments:

        detectionSetting -- A HPeakDetectionSettings object.  The setting index must be between 0 and 127.
        """

        self.comm.execute_command("#AddDetectionsetting", detectionSetting.pack())

    def remove_detection_setting(self, detectionSettingID):
        """Removes a user defined detection setting.
        Settings currently in use on a channel may not be removed.
        
        Keyword Arguments:

        detectionSettingID -- The index of the detection setting to be removed.
                              This must be between 0 and 127.
        """

        argument = "{0}".format(detectionSettingID)
        self.comm.execute_command("#RemoveDetectionSetting", argument)

    def update_detection_setting(self, detectionSetting):
        """Updates an existing detection setting.
        If any channels are currently using the setting, they will be immediately updated.
        
        Keyword Arguments:

        detectionSetting -- A HPeakDetectionSettings object.  The setting ID must match
        an existing setting.
        """

        self.comm.execute_command("#UpdateDetectionSetting", detectionSetting.pack())

    def set_channel_detection_setting_id(self, channel, detectionSettingID):
        """Assigns the detection setting with the specified ID to the specified channel
        
        Keyword Arguments:
        channel -- The instrument channel number.  This number must be between 1
                   and the number of channels on the instrument, inclusive.
        detectionSettingID -- The settingID for the detection setting preset that
                              is to be applied to the specified channel.
        """
        argument = "{0} {1}".format(channel, detectionSettingID)

        self.comm.execute_command("#SetChannelDetectionSettingID", argument)

    def get_channel_detection_setting_id(self, channel):
        """Returns the detection setting ID currently used on the specified channel.
        
        Keyword Arguments:
        channel -- The instrument channel for which the setting ID is to be returned.
        
        Return Value:
        The requested detection setting ID as an integer.
        """
        argument = "{0}".format(channel)
        id = self.comm.execute_command("#GetChannelDetectionSettingId", argument)['content']

        return unpack('H', id)[0]

    def get_all_channel_detection_setting_ids(self):
        """Returns a byte array containing the detection setting IDs currently in use on all channels.
        
        Return Value:
        List of all detection setting IDs, ordered by channel number.
        """
        idList = [];
        ids = bytearray(self.comm.execute_command("#GetAllChannelDetectionSettingIds")['content'])
        for id in ids:
            idList.append(int(id))

        return idList

    def shift_wavelength_by_offset(self, wl_nm, time_of_flight_offset_ns):
        """Get the wavelength resulting from a time of flight offset (SoL). The offset is in nanoseconds and can be positive or negative.

        :param wl_nm: Wavelength (nm)
        :param time_of_flight_offset_ns: Time of Flight Offset (ns) offset_ns = int(2E9 * distance_m * index_of_reflection{=1.4682} / speed_of_light{=299792458.0} )
        :return: wavelength shifted (nm)
        """

        offset_definition = str(wl_nm) + ' ' + str(time_of_flight_offset_ns)
        result = self.comm.execute_command('#ShiftWavelengthByOffset', offset_definition)
        return unpack('d', result['content'])[0]

    def set_channel_sol_compensation_offset(self, channel, compensation_definition):
        """Assigns the SpeedOfFlight compensations to the specified channel

        Keyword Arguments:
        channel -- The instrument channel number.  This number must be between 1
                   and the number of channels on the instrument, inclusive.
        offset_definition -- The list of boundaries in pm and offsets in ns for the SpeedOfFlight compensations that
                              is to be applied to the specified channel.
        """
        if len(compensation_definition) == 0:
            return 'Nothing to apply'

        offset_definition = str(channel) + ' ' + str(len(compensation_definition))
        for (boundary_pm, offset_ns) in compensation_definition:
            # convert boundary from pm to dimensionless
            boundary = int((boundary_pm / 1000 - self.get_wavelength_start()) / self.get_wavelength_delta())

            # valid boundary is between 1202 and 31250
            if 1202 < boundary < 31250:
                offset_definition = offset_definition + ' ' + str(int(offset_ns)) + ' ' + str(int(boundary))

        result = self.comm.execute_command('#setpeakoffsets', offset_definition)
        return result

    def get_channel_sol_compensation_offset(self, channel):
        """Returns current SpeedOfFlight compensations for a specific channel

        Keyword Arguments:
        channel -- The instrument channel for which the offsets is to be returned.

        Return Value:
        List of all SoL compensation offsets for required channel (boundary offset in pm, time in ns)
        """

        argument = "{0}".format(channel)

        '''Command #getpeakoffsets gets the distance related time of flight peak offsets (in ns) for the 
            specified channel. The first two bytes are an UInt16 with the number of pairs. This is 
            followed by each time/boundary offset as UInt32 then UInt16. '''
        result = self.comm.execute_command('#getpeakoffsets', argument)
        content = result['content']

        pairs_num = unpack("H", content[:2])[0]

        ret = []
        pair_len = 6    # summary len of UInt32 and UInt16
        for i in range(pairs_num):
            start = 2 + i * pair_len
            finish = start + pair_len
            offset_ns, boundary = unpack('IH', content[start: finish])
            boundary = 1000 * (self.get_wavelength_start() + self.get_wavelength_delta() * boundary)
            ret.append((boundary, offset_ns))

        return ret

    # ******************************************************************************
    # ACQ API
    # ******************************************************************************

    def get_peaks(self):
        """Get the complete set of peaks for all channels from the instrument.
        
        The peaks are returned as an HACQPeaks object, and also stored in the
        instance variable peaks.  Information about the peaks is contained in 
        the instance variable peaksHeader.
        """
        peakData = self.comm.execute_command('#GetPeaks', '', 0)['content']
        (headerLength,) = unpack('H', peakData[:2])

        self.peaksHeader = HACQPeaksHeader(peakData[:headerLength])
        self.peaks = HACQPeaks(peakData[headerLength:], self.peaksHeader)
        return self.peaks

    def get_raw_spectrum(self, channel=None):
        """Get a complete spectrum for a single channel
        
        Keyword argument:
        channel -- channel number whose spectrum is to be returned.  If no
        channels are specified, or if channel = -1, then all channels are returned,
        """
        if channel is not None:
            argument = "{0}".format(channel)
        else:
            argument = ""
        spectrum = self.comm.execute_command('#GetSpectrum',
                                             argument)['content']

        (headerLength,) = unpack('H', spectrum[:2])
        self.spectrumHeader = HACQSpectrumHeader(spectrum[:headerLength])
        self.spectrum = HACQSpectrum(spectrum[headerLength:],
                                     self.spectrumHeader)

    def get_spectrum(self, channel=None):
        """Get spectral output in dBm power units
        
        Keyword argument:
        channel -- channel for which full spectrum is to be returned.  If None,
        then all channels will be returned.  Note that in order to use streaming,
        the channel must be None.
        """
        if channel is not None:
            self.get_raw_spectrum(channel)
            calSpectrumData = []
            if USE_NUMPY:
                calSpectrumData = (np.array(self.spectrum.data) *
                                   self.invScale[channel - 1]) + self.offset[channel - 1]
            else:
                for index, data in enumerate(self.spectrum.data):
                    calSpectrumData[index] = data * self.invScale[channel - 1] + self.offset[channel - 1]

            return calSpectrumData
        else:
            # use streaming to get all channels if enabled
            if self.spectrumStreamComm != None:
                self.stream_raw_spectrum()
            else:
                # call get_spectrum with no arguments to get all channels
                self.get_raw_spectrum()
            # We need to get the correct channel list so that we can apply the
            # correct calibration values per channel
            if self.spectrumHeader.activeChannelBits != self.activeChannelBits:
                self.activeChannelBits = self.spectrumHeader.activeChannelBits
                self.activeChannelIndices = []
                for channelIndex in range(self.numChannels):
                    if ((self.activeChannelBits >> channelIndex) & 1):
                        self.activeChannelIndices.append(channelIndex)

            if USE_NUMPY:

                channelList = np.array(self.activeChannelIndices)
                spectrumOffsets = np.array(self.offset)[channelList]
                spectrumInvScales = np.array(self.invScale)[channelList]
                calSpectrumData = np.reshape(self.spectrum.data,
                                             (self.spectrum.numChannels, self.spectrum.numPoints))

                calSpectrumData = calSpectrumData.T * spectrumInvScales + spectrumOffsets
            else:
                # Not yet implemented
                calSpectrumData = []

            return calSpectrumData

    def get_active_full_spectrum_channel_numbers(self):
        """Get the list of active full spectrum channels.
        
        Return Value:
        Returns an array of channel indices for which full spectrum data can be
        returned with a call to get_spectrum().
        """
        dataBytes = self.comm.execute_command("#getActiveFullSpectrumDutChannelNumbers")['content']
        channelList = []
        while len(dataBytes):
            channelList.append(unpack('I', dataBytes[:4])[0])
            dataBytes = dataBytes[4:]

        return channelList

    def set_active_full_spectrum_channel_numbers(self, channelList):
        """Set the list of all channels to be returned in calls to get_spectrum()
        
        Keyword Arguments:
        channelList -- A list of all channels for which full spectrum data should
                       be acquired.
        """

        channelString = ''

        for channel in channelList:
            channelString += "{0} ".format(channel)

        self.comm.execute_command("#setActiveFullSpectrumDutChannelNumbers", channelString)

    def set_peak_stream_divider(self, streamingDivider):
        """Sets the streaming divider for the Hyperion Instrument
        
        Keyword argument:
        streamingDivider -- Positive integer value of the streaming divider
        """
        dividerString = "{0}".format(streamingDivider)
        val = self.comm.execute_command('#SetPeakDataStreamingDivider',
                                        dividerString)

    def enable_peak_streaming(self, streamingDivider=1, comm=None):
        """Set up the instrument for streaming peak data.
        
        This must be called in order to initialize the streaming port.
        
        Keyword arguments:
        port -- the port to be used for streaming.  The default is 
                hyperion.STREAM_PORT.
        streamingDivider -- The sampling divider for streaming data.
        """
        self.comm.execute_command('#EnablePeakDataStreaming')
        self.set_peak_stream_divider(streamingDivider)
        if comm == None:
            self.peakStreamComm = HCommTCPSocket(self.comm.ipAddress,
                                                 port=STREAM_PEAKS_PORT)
        else:
            self.peakStreamComm = comm

    def disable_peak_streaming(self):
        """Stops streaming peak data and closes the peakStreamComm object"""

        self.comm.execute_command('#DisablePeakDataStreaming')

        try:
            self.peakStreamComm.close()
            self.peakStreamComm = None
        except AttributeError:
            pass

    def get_peak_streaming_status(self):
        """Get the streaming status and percentage of available stream buffer
        for peak data streaming.
        Return Values:
        streamingEnabled -- Boolean indicating whether streaming is enabled or disabled.
        availableBuffer -- Percentage of the stream buffer that is available.
        """
        availableBuffer = 0
        self.comm.execute_command('#GetPeakDataStreamingStatus')
        streamingEnabled = (unpack('i', self.comm.lastResponse['content'])[0] != 0)
        if streamingEnabled:
            self.comm.execute_command('#GetPeakDataStreamingAvailableBuffer')

            availableBuffer = unpack('i', self.comm.lastResponse['content'])[0]
        return streamingEnabled, availableBuffer

    def stream_peaks(self):
        """Get a single sample of streaming peak data.
        
        The peaks are contained in the instance variable peaks.  
        Information about the peaks is contained in the instance
        variable peaksHeader.
        """
        self.peakStreamComm.read_response()
        peakData = self.peakStreamComm.lastResponse['content']
        (headerLength,) = unpack('H', peakData[:2])
        self.peaksHeader = HACQPeaksHeader(peakData[:headerLength])
        self.peaks = HACQPeaks(peakData[headerLength:], self.peaksHeader)
        return self.peaksHeader.serialNumber

    def set_spectrum_stream_divider(self, streamingDivider):
        """Sets the full spectra streaming divider for the Hyperion Instrument
        
        Keyword argument:
        streamingDivider -- Positive integer value of the streaming divider
        """
        dividerString = "{0}".format(streamingDivider)
        val = self.comm.execute_command('#SetFullSpectrumDataStreamingDivider',
                                        dividerString)

    def enable_spectrum_streaming(self, streamingDivider=1, comm=None):
        """Set up the instrument for streaming full spectra data.
        
        This must be called in order to initialize the streaming port.
        
        Keyword arguments:
        port -- the port to be used for streaming.  The default is 
                hyperion.STREAM_SPECTRA_PORT.
        streamingDivider -- The sampling divider for streaming data.
        """
        self.comm.execute_command('#EnableFullSpectrumDataStreaming')
        self.set_spectrum_stream_divider(streamingDivider)
        if comm == None:
            self.spectrumStreamComm = HCommTCPSocket(self.comm.ipAddress,
                                                     port=STREAM_SPECTRA_PORT)
        else:
            self.spectrumStreamComm = comm

    def disable_spectrum_streaming(self):
        """Stops streaming spectra data and closes the spectraStreamComm object"""
        self.comm.execute_command('#DisableFullSpectrumDataStreaming')

        try:
            self.spectrumStreamComm.close()
            self.spectrumStreamComm = None
        except AttributeError:
            # In case there is no spectrumStreamComm
            pass

    def enable_sensor_streaming(self):
        """Opens the comm port for sensor data streaming
        
        :return: None
        """

        self.sensorStreamComm = HCommTCPSocket(self.comm.ipAddress,port=STREAM_SENSORS_PORT)

    def stream_sensors(self):
        """Stream data from defined sensors on the sensor streaming port.
        
        :return: A HACQSensorData object with the latest data samples.
        """
        self.sensorStreamComm.read_response()
        return HACQSensorData(self.sensorStreamComm.lastResponse['content'])

    def disable_sensor_streaming(self):
        """Closes the comm port for sensor data streaming

        :return: None
        """

        self.sensorStreamComm.close()

    def get_spectrum_streaming_status(self):
        """Get the streaming status and percentage of available stream buffer
        for spectra data streaming.
        Return Values:
        streamingEnabled -- Boolean indicating whether streaming is enabled or disabled.
        availableBuffer -- Percentage of the stream buffer that is available.
        """
        availableBuffer = 0
        self.comm.execute_command('#GetFullSpectrumDataStreamingStatus')
        streamingEnabled = (unpack('i', self.comm.lastResponse['content'])[0] != 0)
        if streamingEnabled:
            self.comm.execute_command('#GetFullSpectrumDataStreamingAvailableBuffer')

            availableBuffer = unpack('i', self.comm.lastResponse['content'])[0]
        return streamingEnabled, availableBuffer

    def stream_raw_spectrum(self):
        """Get a single scan of full spectrum from all channels
        
        """
        self.spectrumStreamComm.read_response()

        spectrumData = self.spectrumStreamComm.lastResponse['content']
        (headerLength,) = unpack('H', spectrumData[:2])
        self.spectrumHeader = HACQSpectrumHeader(spectrumData[:headerLength])
        self.spectrum = HACQSpectrum(spectrumData[headerLength:],
                                     self.spectrumHeader)

    def get_wavelength_start(self):
        """Get the minimum wavelength used in the scanning laser"""

        self.comm.execute_command('#GetUserWavelengthStart')

        return unpack('d', self.comm.lastResponse['content'])[0]

    def get_wavelength_number_of_points(self):
        """Get the number of sampling points used in the scanning laser"""

        self.comm.execute_command('#GetUserWavelengthNumberOfPoints')

        return unpack('i', self.comm.lastResponse['content'])[0]

    def get_wavelength_delta(self):
        """Get the change in wavelength between neighboring points in a scan"""

        self.comm.execute_command('#GetUserWavelengthDelta')

        return unpack('d', self.comm.lastResponse['content'])[0]

    # ******************************************************************************
    # Laser API
    # ******************************************************************************

    def get_available_laser_scan_speeds(self):
        """Get an array of the scan speeds available on the instrument.
        return values:
        scanSpeeds -- An array of available scan speeds
        """

        self.comm.execute_command('#GetAvailableLaserScanSpeeds')

        return array('i', self.comm.lastResponse['content'])

    def set_laser_scan_speed(self, scanSpeed):

        scanSpeedString = "{0}".format(scanSpeed)
        val = self.comm.execute_command('#SetLaserScanSpeed',
                                        scanSpeedString)

        self.scanSpeed = self.get_laser_scan_speed()

    def get_laser_scan_speed(self):
        """Get the curren scan rate of the swept laser."""

        self.comm.execute_command('#GetLaserScanSpeed')

        return unpack('i', self.comm.lastResponse['content'])[0]

    # ******************************************************************************
    # Calibration API
    # ******************************************************************************

    def get_power_cal_offset_scale(self):
        """Gets the offset and scale to be used to convert the fixed point
        spectrum data into dBm units.  Each data point in the spectrum can be
        expressed as dBmValue = rawValue/scale + offset.  The scale and offset
        are channel dependent.
        return values:
        offset -- An array of values that represent the minimum of spectra 
        dBm range.  There is one value for each channel.
        scale -- Array containing the scale values for the spectral range.
        """
        self.comm.execute_command('#GetPowerCalibrationInfo')

        calInfo = array('i', self.comm.lastResponse['content'])
        offset = calInfo[::2]
        scale = calInfo[1::2]
        return offset, scale

    # ******************************************************************************
    # NET API
    # ******************************************************************************

    def get_active_network_settings(self):
        """Return the network IP settings for the instrument"""

        netAddrs = self.comm.execute_command('#GetActiveNetworkSettings')
        netAddrs = netAddrs['content']

        ipAddr = socket.inet_ntoa(netAddrs[0:4])
        mask = socket.inet_ntoa(netAddrs[4:8])
        gateway = socket.inet_ntoa(netAddrs[8:12])
        return dict(ipAddress=ipAddr, netmask=mask, gateway=gateway)

    def get_network_ip_mode(self):
        """Return the network Ip configuration mode, DHCP or Static"""

        ipMode = self.comm.execute_command('#GetNetworkIpMode')['content'].decode('utf-8')

        return ipMode

    def get_static_network_settings(self):
        """Return the static network IP settings for the instrument"""

        netAddrs = self.comm.execute_command('#GetStaticNetworkSettings')
        netAddrs = netAddrs['content']

        ipAddr = socket.inet_ntoa(netAddrs[0:4])
        mask = socket.inet_ntoa(netAddrs[4:8])
        gateway = socket.inet_ntoa(netAddrs[8:12])
        return dict(ipAddress=ipAddr, netmask=mask, gateway=gateway)

    def set_network_ip_mode(self, mode):
        """Set the network IP address acquisition mode to static or dynamic.
        This will disconnect the current comm object.  
        
        Keyword Arguments:
        mode -- string containing the mode type.  Can be static, dynamic, or
                dhcp.
        """
        if mode in ['Static', 'static', 'STATIC']:
            command = '#EnableStaticIpMode'
        elif mode in ['dynamic', 'Dynamic', 'DHCP', 'dhcp']:
            command = '#EnableDynamicIpMode'
        else:
            raise HyperionError('Hyperion Error:  Unknown Network IP Mode requested')
        val = self.comm.execute_command(command)
        self.comm.close()

    def set_static_network_settings(self, ipAddress, netMask, gateway):
        """Set the network settings for static mode.
        If the system is already in static mode, this will close the connection.
        
        Keyword arguments:
        ipAddress:  The static ipV4 ipaddress, as a string in #.#.#.# format
        netMask:  the network mask as a string. 
        gateway:  the network gateway address as string
        """
        addressArg = ipAddress + ' ' + netMask + ' ' + gateway

        ipMode = self.get_network_ip_mode()
        val = self.comm.execute_command('#SetStaticNetworkSettings', addressArg)
        if ipMode == 'STATIC':
            self.comm.close()
    # ******************************************************************************
    # HOST API
    # ******************************************************************************

    def set_instrument_utc_date_time(self, year, month, day, hour, min, sec = 0):
        """Sets the instrument date and time in UTC.
        You must ensure that the instrument booted with NTP server disabled or
        the date/time will be rewritten.
        """
        dateString = "{0} {1} {2} {3} {4} {5}".format(year, month, day, hour, min, sec)
        self.comm.execute_command("#SetInstrumentUtcDateTime", dateString)

    def get_instrument_utc_date_time(self):
        """Get the instrument date and time in UTC.

        Returns a time tuple with the following items in order:
            year -- Integer value of the current year.
            month -- Integer value of the current month.
            day -- Integer value of the current day.
            hour -- Integer value of the current hour.
            minute -- Integer value of the current minute.
            second -- Integer value of the current second.
        """
        dateData = self.comm.execute_command("#GetInstrumentUtcDateTime")['content']


        return unpack('HHHHHH', dateData)

    def set_ntp_enabled(self, enabled = True):
        """Set whether or not a remote Network Time Protocol (NTP) server is used to keep the
        system time synchronized.

        Keyword Arguments:
        enabled -- Boolean value.  Set to True to enable NTP, and False to disable.
        """
        if enabled:
            argument = '1'
        else:
            argument = '0'

        self.comm.execute_command("#SetNtpEnabled", argument)

    def get_ntp_enabled(self):
        """Get whether or not a remote Network Time Protocol (NTP) server is used to keep the
        system time synchronized.

        Returns a boolean value indicating the enabled status.

        """

        return unpack('I', self.comm.execute_command("#GetNtpEnabled")['content'])[0] > 0

    def set_ntp_server(self, ntpServerIP):
        """Set the IP address of the remote Network Time Protocol (NTP) server used to keep
        the system time synchronized.

        Keyword Arguments:

        ntpServerIP -- String containing the IP Address of the NTP server.
                       URLs are not supported.
        """
        self.comm.execute_command("#SetNtpServer", ntpServerIP)

    def get_ntp_server(self):
        """Get the IP address of the remote Network Time Protocol (NTP) server used to keep
        the system time synchronized.

        Returns String containing the IP Address of the NTP server.
        """
        return self.comm.execute_command("#GetNtpServer")['content'].decode('utf-8')

    def set_ptp_enabled(self, enabled = True):
        """Set whether or not a Precision Time Protocol (PTP) client is used to keep the system
        time synchronized to a local clock.

        Keyword Arguments:

        enabled --  Boolean value.  Set to true to enable PTP.
        """
        if enabled:
            argument = '1'
        else:
            argument = '0'

        self.comm.execute_command("#SetPtpEnabled", argument)

    def get_ptp_enabled(self):
        """Get whether or not a Precision Time Protocol (PTP) client is used to keep the system
        time synchronized to a local clock.

        returns a boolean value indicating the enabled status.
        """

        return unpack('I', self.comm.execute_command("#GetPtpEnabled")['content'])[0] > 0

    # ******************************************************************************
    # Sensors API
    # ******************************************************************************

    def add_sensor(self, sensorName, sensorModel, channel, wavelengthBand, calFactor, distance=0):
        """Add a sensor to the hyperion instrument.  Added sensors will stream data over the sensor streaming port.
        :param sensorName: Sensor name.  This is an arbitrary string provided by user.
        :param sensorModel: Sensor model.  This must match the specific model, currently either os7510 or os7520.
        :param channel: Instrument channel on which the sensor is present.  First channel is 1.
        :param wavelengthBand: The wavelength band of the sensor.
        :param calFactor: The calibration constant for the sensor.
        :param distance: Fiber length from sensor to interrogator, in meters, integer.
        :return: None
        """
        argument = '{0} {1} {2} {3} {4} {5}'.format(sensorName, sensorModel, channel, distance, wavelengthBand, calFactor)
        self.comm.execute_command("#AddSensor", argument)

    def get_sensor_names(self):
        """
        Get the list of user defined names for sensors currently defined on the instrument.
        :return: Array of strings containing the sensor names
        """
        response = self.comm.execute_command('#GetSensorNames')
        return response['message'].split(' ')

    def export_sensors(self):
        """Returns all configuration data for all sensors that are currently defined on the instrument.
        
        :return: Array of dictionaries containing the sensor configuration
        """
        response = self.comm.execute_command('#ExportSensors')

        sensor_export = response['content']
        headerVersion, numSensors = unpack('HH',sensor_export[:4])
        sensor_export = sensor_export[4:]
        sensor_configs = []

        for sensorNum in range(numSensors):
            sensor_config = dict()
            sensor_config['version'], = unpack('H', sensor_export[:2])
            sensor_export = sensor_export[2:]

            sensor_config['id'] = list(bytearray(sensor_export[:16]))
            sensor_export = sensor_export[16:]

            name_length, = unpack('H',sensor_export[:2])

            sensor_export = sensor_export[2:]
            sensor_config['name'] = sensor_export[:name_length]
            sensor_export = sensor_export[name_length:]

            model_length, = unpack('H', sensor_export[:2])
            sensor_export = sensor_export[2:]
            sensor_config['model'] = sensor_export[:model_length]
            sensor_export = sensor_export[model_length:]

            sensor_config['channel'], = unpack('H', sensor_export[:2])
            sensor_config['channel'] += 1
            sensor_export = sensor_export[2:]

            sensor_config['distance'], = unpack('d',sensor_export[:8])

            #drop 2 bytes for reserved field
            sensor_export = sensor_export[10:]

            detail_keys = ('wavelengthBand',
                        'calFactor',
                        'rcGain',
                        'rcThresholdHigh',
                        'rcThresholdLow')

            sensor_details = dict(list(zip(detail_keys,unpack('ddddd',sensor_export[:40]))))
            sensor_export = sensor_export[40:]
            sensor_config.update(sensor_details)
            sensor_configs.append(sensor_config)
        return sensor_configs

    def remove_sensors(self,sensor_names=None):
        """Removes Sensors by name
        
        :param sensor_names: This can be a single sensor name string or a list of sensor names strings.  If omitted,
        all sensors are removed.
        :return: None
        """

        if sensor_names is None:
            sensor_names = self.get_sensor_names()
        elif type(sensor_names) == str:
            sensor_names = [sensor_names]

        for name in sensor_names:
            self.comm.execute_command('#removeSensor',name)

    def save_sensors(self):
        """Saves all sensors to persistent storage.
        
        :return: None
        """

        self.comm.execute_command('#saveSensors')

# ******************************************************************************
# COMM Classes
# ******************************************************************************


class HComm:
    """Class that defines a generic communication interface.  Any comm object
    to be used by the Hyperion class needs to implement this complete
    interface.
    """

    def connect(self):
        pass

    def close(self):
        pass

    def settimeout(self, timeout=1000):
        pass

    def execute_command(self, command, argument='', requestOptions=0):
        content = None
        message = None
        return dict(content=content, message=message)

    def write_command(self, command, argument='', requestOptions=0):
        pass

    def read_response(self):
        pass

    def read_data(self, dataLength):
        pass


class HCommTCPSocket:
    """Class that defines a TCP Comm socket interface to the instrument.  This
    implements the interface outlined in the HComm object.
    
    instance variables:
    ipAddress -- string containing the ipV4 address of the hyperion
                 instrument. e.g. '10.0.0.199'
    port -- TCP port that is used for sending and receiving data.
    timeout -- Timeout value for TCP operations, in milliseconds. 
    connected -- status of the connection.  True means their is an active
                 TCP connection.
    commSocket -- TCP socket object that interfaces to the instrument.
    lastResponse -- Most recent response read from the instrument,
                    implemented as a dictionary with keywords "content" and
                    "message"
    
    """

    def __init__(self, ipAddress, port=COMMAND_PORT,
                 timeout=DEFAULT_TIMEOUT):
        """Initializes a Hyperion TCP Communication instance.
        
        Keyword arguments:
        ipAddress -- string containing the ipV4 address of the hyperion
                     instrument, e.g. '10.0.0.199'
        port -- TCP port that is used for sending and receiving data.
                       The default is hyerion.COMMAND_PORT
        timeout -- Timeout value for TCP operations, in milliseconds.  The
                   The default is hyperion.DEFAULT_TIMEOUT

        """
        self.connected = False
        self.ipAddress = ipAddress
        self.port = port
        self.readBuffer = b''

        self.timeout = timeout
        self.connect()

    def connect(self):
        """Open the TCP command socket to the instrument"""

        if not self.connected:
            self.commSocket = socket.socket(socket.AF_INET,
                                            socket.SOCK_STREAM)
            self.set_timeout(self.timeout)
            try:
                self.commSocket.connect((self.ipAddress, self.port))
            except socket.error as e:
                raise HyperionError('Unable to connect to instrument command port.')
            except socket.timeout:
                raise HyperionError("Connection timed out")
            else:
                self.connected = True
        else:
            raise HyperionError('Connection appears to already be open')

    def close(self):
        """Closes the TCP sockets to the instrument"""
        if self.connected:
            try:
                self.commSocket.shutdown(socket.SHUT_RDWR)
                self.commSocket.close()
            except Exception:
                pass

            self.connected = False

    def set_timeout(self, timeout):
        """Set the timeout value for all subsequent TCP operations"""
        self.commSocket.settimeout(timeout / 1000.0)

    def execute_command(self, command, argument='', requestOptions=0):
        """Execute a Hyperion command, and return the latest content.
        
        After execution, the returned data is contained in the instance
        variable lastResponse, which is a tuple containing the content and the
        message.
        
        The return value is a tuple containing (content,message), as returned
        by the instrument.
        
        Keyword arguments:
        command -- string containing the command to be sent to the instrument.
        argument -- string containing the arguments to the command.
        requestOptions -- byte flags that determine the type of data returned
                          by the instrument.
        """
        if self.connected:
            self.write_command(command, argument, requestOptions)
            self.read_response()
            return self.lastResponse
        else:
            raise HyperionError('Instrument not connected, try using connect()')
            return None

    def write_command(self, command, argument, requestOptions):
        """Write a command to the hyperion instrument.
        
        Keyword arguments:
        command -- string containing the command to be sent to the instrument.
        argument -- string containing the arguments to the command.
        """
        if self.connected:
            self.lastCommand = command
            self.lastArgument = argument
            headerOut = pack('BBHI', requestOptions, 0,
                             len(command), len(argument))
            self.lastHeaderOut = headerOut
            try:
                self.commSocket.sendall(headerOut)  # headerOut is already bytes
                self.commSocket.sendall(bytes(command, 'utf-8'))
                self.commSocket.sendall(bytes(argument, 'utf-8'))
            except socket.error as e:
                raise HyperionError(e)
            except socket.timeout:
                raise HyperionError("Connection timed out")
        else:
            raise HyperionError('Instrument not connected, try using connect()')

    def read_response(self):
        """Read a response from the Hyperion instrument.
        
        This reads the content and message from the Hyperion, and stores
        the result in the instance variable lastResponse.
        """
        headerIn = self.read_data(HEADER_LENGTH)
        self.lastHeaderIn = headerIn
        headerStruct = unpack('BBHI', headerIn)
        status, responseType, messageLength, contentLength = headerStruct
        if messageLength > 0:
            message = self.read_data(messageLength).decode("utf-8")
        else:
            message = ''
        if status != SUCCESS:
            self.readBuffer = b''
            raise HyperionError
        else:
            if contentLength > 0:
                content = self.read_data(contentLength)
            else:
                content = ''
        response = dict(message=message, content=content)
        self.lastResponse = response
        return 0

    def read_data(self, dataLength):
        """Reads data from the TCP port.
        
        Keyword arguments:
        dataLength -- The total amount of data to be retrieved.

        """
        data = self.readBuffer
        while len(data) < dataLength:
            new_data_bytes = self.commSocket.recv(RECV_BUFFER_SIZE)
            data += new_data_bytes
        dataOut = data[:dataLength]
        self.readBuffer = data[dataLength:]

        return dataOut


# ******************************************************************************
# ACQ Helper Classes
# ******************************************************************************


class HACQStreamer(Process):
    """This is a class that implements multi-processor buffered streaming.
    Once this object is instantiated, the instrument will begin streaming in
    a background process, and return data when requested, freeing up the 
    main process for other tasks.
    
    Instance variables:
    There are no instance variables that are intended to be publicly
    accessible.
    """

    def __init__(self, comm, channelList, bufferLength=1000):
        """Initializes a HACQStreamer Process
        
        keyword arguments:
        comm -- A communications object that implements the HComm interface.
        This needs to already be active and connected to the streaming port.
        channelList -- a list of channels for which the peaks will be returned
        bufferLength -- Number of datapoints kept in the output data queue
        """
        Process.__init__(self, target=self.stream)
        self.channelList = channelList
        self.comm = comm
        self.errorCount = 0
        self.commandQueue = MPQueue()
        self.dataQueue = MPQueue(bufferLength)
        self.status = True
        raise HyperionError("initial PID: ", os.getpid())
        raise HyperionError("Starting Streaming Process")
        self.start()

    def stream(self):
        """This function is the process that is run in the spawned process"""
        raise HyperionError("Streaming Process Started")
        raise HyperionError("Streaming process PID:", os.getpid())
        while 1:
            try:
                self.comm.read_response()
            except HyperionError as e:
                raise HyperionError(e)
                self.errorCount += 1
                if errorCount >= MAX_STREAMING_ERRORS:
                    self.status = False
            else:
                peakData = self.comm.lastResponse['content']
                (headerLength,) = unpack('H', peakData[:2])
                peaksHeader = HACQPeaksHeader(peakData[:headerLength])
                peaks = HACQPeaks(peakData[headerLength:], peaksHeader)
                timeStamp = (peaksHeader.timeStampInt +
                             1e-9 * peaksHeader.timeStampFrac)
                dataOut = {}

                dataOut['timeStamp'] = timeStamp
                dataOut['serialNumber'] = peaksHeader.serialNumber
                for channel in self.channelList:
                    dataOut[channel] = peaks.get_channel(channel)
                if self.status:
                    try:
                        self.dataQueue.put_nowait(dataOut)
                    except queue.Full:
                        # Skip sample if queue is at maximum length
                        raise HyperionError("Skipped data due to full queue.  SN: ", \
                            peaksHeader.serialNumber)

            try:
                command = self.commandQueue.get_nowait()
            except queue.Empty:
                pass
            else:
                command = command.lower()
                if command == 'stop':
                    self.status = False
                    raise HyperionError("Stopping stream")
                    break
                elif command == 'pause':
                    self.status = False
                elif command == 'resume':
                    self.status = True

    def stop_streaming(self):
        """This will stop all streaming and end the process.  
        A new HACQStreamer object will need to be created to start streaming 
        again
        """
        self.commandQueue.put('stop')

    def pause_streaming(self):
        """This will stop streaming and just dump samples until streaming 
        is resumed or stopped
        """
        self.commandQueue.put('pause')

    def resume_streaming(self):
        """This will resume streaming if it has been paused."""
        self.commandQueue.put('resume')

    def get_data(self):
        """This will return one data sample of peak data from the queue  data
        format is a dictionary consisting of the following keywords:
        timeStamp -- Timestamp for the data 
        serialNumber -- serial number for the data
        ch#_1...ch#_n -- A number for each channel in the list that is passed
                         to the initializer.
        """
        try:
            dataOut = self.dataQueue.get_nowait()
        except queue.Empty:
            return None
        else:
            return dataOut

    def get_all_data(self):
        """This will return all data available from the queue
        Data is returned as a list, with each element in the list being a list
        in the format described in get_data"""
        data = list([])
        while 1:
            try:
                data.append(self.dataQueue.get_nowait())
            except queue.Empty:
                return data


class HACQPeaksHeader:
    """Class that encapsulates the header returned when acquiring peaks.
    
    instance variables:
    length -- the length of the header in bytes.
    version -- number specifying the header version used.
    serialNumber -- a number assigned to each sample, incremented by one
                    for every set of peaks acquired by the instrument.
    timestampInt -- The integer portion of the timestamp, in seconds.
    timestampFrac -- the fractional portion of the timestamp, in nanoseconds.
    """

    def __init__(self, headerData):
        """Initializes the header by parsing the binary data.
        
        Keyword arguments:
        headerData -- string of binary data returned by the instument.
        """
        (self.length, self.version, self.reserved, self.serialNumber,
         self.timeStampInt, self.timeStampFrac) = unpack('HHIQII',
                                                         headerData[:24])
        self.peakCounts = array('H', headerData[24:])


class HACQPeaks(array):
    """Class that encapsulates the peak data returned from the Hyperion.
    
    This is a subclass of an array object.
    
    instance variables:
    channelPeaksInds -- list of tuples that contain the start and end indices
                        for all channels.
    """

    def __new__(cls, peakData, peaksHeader):
        v = super(HACQPeaks, cls).__new__(cls, 'd')
        return v

    def __init__(self, peakData, peaksHeader):
        """Initializes the peak data by parsing the binary data string.
        
        Keyword arguments:
        peaksHeader -- a HACQPeaksHeader object that contains the information 
                       from the header.
        peakData -- string of binary data containing all peak information as 
                    returned by the instrument.
        """
        self.fromstring(peakData)
        self.channelPeaksInds = list(range(0, len(peaksHeader.peakCounts)))
        startPeak = 0
        for index, peakCount in enumerate(peaksHeader.peakCounts):
            endPeak = startPeak + peakCount
            self.channelPeaksInds[index] = (startPeak, endPeak)
            startPeak = endPeak

    def get_channel(self, channelNum):
        """Return all of the peaks from a given channel
        
        Keyword arguments:
        channelNum -- integer in range 1 to channel count specifying which
                      channel to return the data for.
        """
        startPeak, endPeak = self.channelPeaksInds[channelNum - 1]
        return self[startPeak:endPeak]


class HACQSpectrumHeader:
    """Class that encapsulates the header returned when acquiring a spectrum.
    
    instance variables:
    length -- the length of the header in bytes.
    version -- number specifying the header version used.
    serialNumber -- a number assigned to each sample, incremented by one
                    for every set of peaks acquired by the instrument.
    timestampInt -- The integer portion of the timestamp, in seconds.
    timestampFrac -- the fractional portion of the timestamp, in nanoseconds.
    startWavelength -- Wavelength corresponding to the first point in the
                       spectrum.
    wavelengthIncrement -- Wavelength increment for each subsequent datapoint.
    numChannels -- The number of channels returned after this header 
    numPoints -- Total number of datapoints per channel in spectrum
    """

    def __init__(self, headerData):
        """Initializes the header by parsing the binary data.
        
        Keyword arguments:
        headerData -- string of binary data returned by the instument.
        """
        (self.length, self.version, self.reserved, self.serialNumber,
         self.timeStampInt, self.timeStampFrac, self.startWavelength,
         self.wavelengthIncrement, self.numPoints, self.numChannels,
         self.activeChannelBits) = unpack('HHIQIIddIHH', headerData)


class HACQSpectrum:
    """Class that encapsulates spectrum data returned from hyperion.
    
    instance variables:
    data -- An array containing the spectrum data.  If a calFunction is
            specified in the initializer, then this will be a numpy array.
    startWavelength -- Wavelength corresponding to the first point in the
                       spectrum.
    wavelengthIncrement -- Wavelength increment for each subsequent datapoint.
    numPoints -- Total number of datapoints in spectrum
    """

    def __init__(self, spectrumData, spectrumHeader):
        """Initializer for the HACQSpectrum object.Keyword arguments:
        spectrumData -- Raw spectrum data returned from instrument
        spectrumHeader -- HACQSpectrumHeader object containing parsed spectrum 
                          header data.

        """
        self.data = array('H', spectrumData)
        self.startWavelength = spectrumHeader.startWavelength
        self.wavelengthIncrement = spectrumHeader.wavelengthIncrement
        self.numPoints = spectrumHeader.numPoints
        self.numChannels = spectrumHeader.numChannels

class HACQSensorData:
    """Class that encapsulates sensor data streamed from hyperion
    
    """
    def __init__(self, streamingData):
        header = namedtuple('header','headerLength status bufferPercentage reserved serialNumber timestampInt timestampFrac')

        self.header = header._make(unpack('HBBIQII',streamingData[:24]))

        self.data = array('d',streamingData[self.header.headerLength:])

class HPeakDetectionSettings:
    """Class that encapsulates the settings that describe peak detection for a
    hyperion channel.
    
    instance variables:
    index -- The numerical index for the setting.  This is how the setting is 
             referenced when interfacing with the instrument.
    name -- A string containing a name for the settings
    description -- A longer description of the use for these settings
    boxcarLength -- The length of the boxcar filter, in units of pm
    diffFilterLength -- The length of the difference filter, in units of pm
    lockout -- The spectral length, in pm, of the lockout period
    ntvPeriod -- The length, in pm, of the noise threshold voltage period.
    threshold -- The normalized threshold for detecting peaks/valleys
    mode -- This is either 'Peak' or 'Valley'
    """

    def __init__(self, settingID=0, name='', description='',
                 boxcarLength=0, diffFilterLength=0,
                 lockout=0, ntvPeriod=0, threshold=0, mode='Peak'):

        self.settingID = settingID
        self.name = name
        self.description = description
        self.boxcarLength = boxcarLength
        self.diffFilterLength = diffFilterLength
        self.lockout = lockout
        self.ntvPeriod = ntvPeriod
        self.threshold = threshold
        self.mode = mode

    @classmethod
    def from_binary_data(cls, detectionSettingsData):

        (settingID, nameLength) = unpack('BB', detectionSettingsData[:2])
        detectionSettingsData = detectionSettingsData[2:]

        name = detectionSettingsData[: nameLength].decode("utf-8")
        detectionSettingsData = detectionSettingsData[nameLength:]

        # (descriptionLength,) = unpack('B', detectionSettingsData[0])
        descriptionLength = detectionSettingsData[0]
        detectionSettingsData = detectionSettingsData[1:]

        description = detectionSettingsData[: descriptionLength].decode("utf-8")

        (boxcarLength, diffFilterLength, lockout,
         ntvPeriod, threshold, mode) = \
            unpack('HHHHiB', detectionSettingsData[descriptionLength:(descriptionLength + 13)])
        # Use _remainingData in case more than one preset is contained in detectionSettingsData
        remainingData = detectionSettingsData[(descriptionLength + 13):]

        if (mode == 0):
            mode = 'Valley'
        else:
            mode = 'Peak'

        return cls(settingID, name, description, boxcarLength, diffFilterLength,
                   lockout, ntvPeriod, threshold, mode), remainingData

    def pack(self):

        if self.mode == 'Peak':
            modeNumber = 1
        else:
            modeNumber = 0

        packString = "{0} '{1}' '{2}' {3} {4} {5} {6} {7} {8}".format(
            self.settingID, self.name, self.description, self.boxcarLength,
            self.diffFilterLength, self.lockout, self.ntvPeriod,
            self.threshold, modeNumber)

        return packString


class HyperionError(Exception):
    """Exception class for encapsulating error information from Hyperion.
    """

    # changed to reflect to error codes
    def __init__(self, message):
        self.string = message

    def __str__(self):
        return repr(self.string)


if __name__ == '__main__':
    import datetime

    h1 = Hyperion('10.0.0.55')
    dropped = False
    h1.enable_peak_streaming()

    channelNum = 1

    raise HyperionError('\n\nSYSTEM API')
    raise HyperionError('instrument_name', h1.get_instrument_name())
    raise HyperionError('serial_number', h1.get_serial_number())
    raise HyperionError('library_version', h1.get_library_version())
    raise HyperionError('version', h1.get_version())
    raise HyperionError('is_ready', h1.is_ready())

    raise HyperionError('\n\nDETECTION API')
    raise HyperionError('channel_count', h1.get_channel_count())
    raise HyperionError('max_peak_count_per_channel', h1.get_max_peak_count_per_channel())
    raise HyperionError('available_detection_settings')
    settings = h1.get_available_detection_settings()
    for cur in settings:
        raise HyperionError('\t', cur.pack())
    raise HyperionError('all_channel_detection_setting_ids', h1.get_all_channel_detection_setting_ids())

    raise HyperionError('\n\nACQ API')
    h1.get_peaks()

    raise HyperionError('active_full_spectrum_channel_numbers', h1.get_active_full_spectrum_channel_numbers())
    raise HyperionError('peak_streaming_status', h1.get_peak_streaming_status())
    raise HyperionError('spectrum_streaming_status', h1.get_spectrum_streaming_status())
    raise HyperionError('wavelength_start', h1.get_wavelength_start())
    raise HyperionError('wavelength_number_of_points', h1.get_wavelength_number_of_points())
    raise HyperionError('wavelength_delta', h1.get_wavelength_delta())

    cur_spectrum_power = h1.get_spectrum(1)
    wl_start = h1.wavelengthStart
    wl_step = h1.wavelengthDelta

    raise HyperionError('Spectrum max %.2f on %.3fnm' % (max(cur_spectrum_power), wl_start + np.argmax(cur_spectrum_power)*wl_step))


    raise HyperionError('\n\nLASER API')
    raise HyperionError('available_laser_scan_speeds', h1.get_available_laser_scan_speeds())
    raise HyperionError('laser_scan_speed', h1.get_laser_scan_speed())
    raise HyperionError('instrument_utc_date_time', h1.get_instrument_utc_date_time())
    raise HyperionError('ptp_enabled', h1.get_ptp_enabled())

    raise HyperionError('\n\nCALIBRATION API')
    raise HyperionError('power_cal_offset_scale', h1.get_power_cal_offset_scale())

    raise HyperionError('\n\nNET API')
    raise HyperionError('active_network_settings', h1.get_active_network_settings())
    raise HyperionError('network_ip_mode', h1.get_network_ip_mode())
    raise HyperionError('static_network_settings', h1.get_static_network_settings())

    raise HyperionError('\n\nHOST API')
    raise HyperionError('instrument_utc_date_time', h1.get_instrument_utc_date_time())
    raise HyperionError('local PC utc_date_time', datetime.datetime.utcnow())
    raise HyperionError('ntp_enabled', h1.get_ntp_enabled())
    raise HyperionError('ntp_server', h1.get_ntp_server())
    raise HyperionError('ptp_enabled', h1.get_ptp_enabled())

    raise HyperionError('\n\nSENSOR API')
    raise HyperionError('export_sensors', h1.export_sensors())
    raise HyperionError('get_sensor_names', h1.get_sensor_names())

    h1.disable_peak_streaming()
    h1.comm.close()

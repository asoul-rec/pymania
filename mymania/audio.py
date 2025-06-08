import asyncio
import logging
import struct
import array
import threading
from collections import deque
import time
import math
from numbers import Real
from typing import Optional, Union
import concurrent.futures

import av
import sounddevice as sd

# Format constants
SAMPLE_FMTS_DATA = {
    'fltp': {'type_code': 'f', 'dtype': 'float32', 'min_val': -1.0, 'max_val': 1.0, 'zero_val': 0.0},
    's16': {'type_code': 'h', 'dtype': 'int16', 'min_val': -32768, 'max_val': 32767, 'zero_val': 0},
}


class AudioPlayer:
    sample_fmt: str
    type_code: str
    dtype: str
    min_val: Real
    max_val: Real
    zero_val: Real
    _pa_ts_offset: Optional[float] = None  # Portable Audio Timestamp Offset comparing with time.perf_counter()
    song_start_time: Optional[float] = None  # Start time of the song in the stream's time base
    real_latency: Optional[float] = None  # Real latency of the stream, set after starting the stream
    _device_info: Optional[dict] = None  # Device info from sounddevice
    _hostapi: Optional[str] = None  # Host API name from sounddevice

    def __init__(self, sample_rate: int, *, channels: int = 2, sample_fmt: str = 's16', latency='low'):
        self.sample_rate = sample_rate
        if channels != 2:
            raise ValueError("AudioPlayer currently supports only stereo output (2 channels).")
        self.channels = channels
        self.layout = 'stereo'

        try:  # Unpack format data into instance variables
            self.sample_fmt = sample_fmt
            self.__dict__.update(SAMPLE_FMTS_DATA[sample_fmt])
        except KeyError:
            raise ValueError(f"Unsupported sample format: {sample_fmt}")

        self._resampler = av.AudioResampler(format=self.sample_fmt, layout=self.layout,
                                            rate=self.sample_rate)
        self.latency = latency
        self._stream: Optional[sd.RawOutputStream] = None
        self.is_playing_song = False
        self.song: Optional["AudioFile"] = None
        self._song_reading_lock = threading.Lock()
        self._active_sfx: deque = deque()  # Each item: (sfx_data_array, current_play_pos_frames, trigger_time)
        self._sfx_lock = threading.Lock()

    async def load_song(self, song, play_now: bool = True):
        self.song = AudioFile(song)
        await self.song.open(resampler=self._resampler)
        if play_now and self._stream and self._stream.active:
            self.is_playing_song = True

    def _clip_sample(self, sample):
        if sample > self.max_val:
            return self.max_val
        if sample < self.min_val:
            return self.min_val
        return sample

    def _audio_callback(self, outdata, samples: int, time_info, status):
        if self._hostapi == "Windows WDM-KS":
            playback_time = time_info.currentTime  # fix for Windows WDM-KS: OutputBufferDacTime is not absolute
        else:
            playback_time = time_info.outputBufferDacTime
        if self._pa_ts_offset is None:
            # Calculate the offset once when the callback is first invoked
            _pa_ts_offset = time.perf_counter() - playback_time
            if 0 <= _pa_ts_offset < 0.001:
                self._pa_ts_offset = 0
            else:
                self._pa_ts_offset = _pa_ts_offset
            print("PA Timestamp Offset:", self._pa_ts_offset, playback_time)
        if status:
            print("Audio Callback Status:", status, flush=True)

        # 1. Create a temporary buffer for mixing, using array.array
        total_samples = samples * self.channels
        # Initialize with silence (self.zero_val)
        outdata[:] = struct.pack(self.type_code, self.zero_val) * total_samples
        outdata_view = memoryview(outdata).cast(self.type_code, (total_samples,))

        # 2. Mix Song
        with self._song_reading_lock:
            if self.song is not None:
                song_data = None
                if self.is_playing_song:
                    # try:
                    #     _read_future = self.song.read_thread_safe(samples)
                    #     song_data = _read_future.result(timeout=samples / self.sample_rate * 0.8)
                    # except TimeoutError:
                    #     logging.warning("Timeout while reading song data, continuing with silence.")
                    # except (concurrent.futures.CancelledError, RuntimeError):
                    #     pass
                    song_data = self.song.read_nowait(samples)
                if song_data is None:
                    if self.song.container is None:
                        self.is_playing_song = False
                else:
                    if (pts := self.song.last_read_pts) is not None:
                        self.song_start_time = playback_time + self._pa_ts_offset - pts
                    else:
                        self.song_start_time = None
                    # t = time.perf_counter() - self._pa_ts_offset
                    # print(f"dac time, {time_info.outputBufferDacTime:.6f}, current time {time_info.currentTime:.6f}, "
                    #       f"{float(self.song.last_read_pts):.3f}, start {self.song_start_time}", flush=True)
                    for i, sample_value in enumerate(song_data):
                        outdata_view[i] = sample_value // 2

        # 3. Mix Sound Effects
        temp_float_sfx_mix = [0.0] * total_samples  # Mix SFX in float for safety
        sfx_to_remove_from_deque = []  # Store (data, pos, time) tuples of sfx to remove

        with self._sfx_lock:
            num_active_sfx = len(self._active_sfx)
            if num_active_sfx > 0:
                # Identify latest SFX (last in deque) and others
                latest_sfx_item = self._active_sfx[-1]
                other_sfx_items = [self._active_sfx[i] for i in range(num_active_sfx - 1)]

                # Process "latest" SFX
                sfx_data, sfx_pos_frames, trigger_time = latest_sfx_item
                sfx_total_frames = len(sfx_data) // self.channels
                remaining_sfx_frames = sfx_total_frames - sfx_pos_frames
                frames_to_read_sfx = min(samples, remaining_sfx_frames)

                if frames_to_read_sfx > 0:
                    start_idx_sfx = sfx_pos_frames * self.channels
                    for i in range(frames_to_read_sfx * self.channels):
                        temp_float_sfx_mix[i] += sfx_data[start_idx_sfx + i] * 0.6  # Latest at 60%
                    # Update position for the item in the deque
                    self._active_sfx[-1] = (sfx_data, sfx_pos_frames + frames_to_read_sfx, trigger_time)

                if (sfx_pos_frames + frames_to_read_sfx) >= sfx_total_frames:
                    sfx_to_remove_from_deque.append(latest_sfx_item)  # Mark for removal

                # Process "other" SFX
                num_other_sfx = len(other_sfx_items)
                if num_other_sfx > 0:
                    volume_per_other_sfx = 0.1 / num_other_sfx  # Share 10% volume equally

                    for i_other, other_sfx_item in enumerate(other_sfx_items):
                        sfx_data_o, sfx_pos_frames_o, trigger_time_o = other_sfx_item
                        sfx_total_frames_o = len(sfx_data_o) // self.channels
                        remaining_sfx_frames_o = sfx_total_frames_o - sfx_pos_frames_o
                        frames_to_read_sfx_o = min(samples, remaining_sfx_frames_o)

                        if frames_to_read_sfx_o > 0:
                            start_idx_sfx_o = sfx_pos_frames_o * self.channels
                            for j in range(frames_to_read_sfx_o * self.channels):
                                temp_float_sfx_mix[j] += sfx_data_o[start_idx_sfx_o + j] * volume_per_other_sfx
                            # Update position for the item in the deque
                            self._active_sfx[i_other] = (
                                sfx_data_o, sfx_pos_frames_o + frames_to_read_sfx_o, trigger_time_o)

                        if (sfx_pos_frames_o + frames_to_read_sfx_o) >= sfx_total_frames_o:
                            sfx_to_remove_from_deque.append(other_sfx_item)

            if sfx_to_remove_from_deque:
                # Create a set of trigger times for fast O(1) lookups
                finished_sfx_times = {item[2] for item in sfx_to_remove_from_deque}

                # Rebuild the deque by filtering out the finished SFX.
                # This is generally safer and clearer than removing from a list in a loop.
                self._active_sfx = deque(item for item in self._active_sfx if item[2] not in finished_sfx_times)

        # Add accumulated float SFX mix to the main mix_buffer_array
        # This assumes mix_buffer_array is of the target type (e.g. float32 or int16)
        for i in range(total_samples):
            outdata_view[i] = self._clip_sample(
                (int if self.type_code == 'h' else float)(outdata_view[i] + temp_float_sfx_mix[i] * self.max_val))

        # 4. Final Clipping (already done per sample during accumulation if careful)
        # If direct accumulation into mix_buffer_array:
        for i in range(total_samples):
            outdata_view[i] = self._clip_sample(outdata_view[i])

    def start_stream(self, **kwargs):
        if self._stream is not None and self._stream.active:
            logging.warning("Stream already active.")
            return
        try:
            stream_kwargs = {
                'samplerate': self.sample_rate,
                'channels': self.channels,
                'dtype': self.dtype,  # Use the string like 'float32'
                'callback': self._audio_callback,
                'latency': self.latency,
            }
            stream_kwargs.update(kwargs)
            self._stream = sd.RawOutputStream(**stream_kwargs)
            self.real_latency = self._stream.latency  # Store the real latency for reference
            device_info = self._device_info = sd.query_devices(self._stream.device)
            self._hostapi = sd.query_hostapis()[device_info['hostapi']]['name']
            print("Audio stream will start, real latency:", self.real_latency)
            self._stream.start()
        except Exception as e:
            print(f"Error starting audio stream: {e}", flush=True)
            self._stream = None

    async def stop_stream(self):
        await self.song.close()
        if self._stream is not None and self._stream.active:
            with self._song_reading_lock, self._sfx_lock:
                self.is_playing_song = False
                self._active_sfx.clear()
                self._stream.close()
            self._stream = None
            print("Audio stream stopped.", flush=True)
        else:
            print("Stream not active or not initialized.", flush=True)

    def resume_song(self):
        assert self._stream and self._stream.active, "Cannot resume song: Stream not active."
        assert self.song, "Cannot resume song: No song loaded."
        assert not self.is_playing_song, "Cannot resume song: Song is already playing."
        self.is_playing_song = True

    def play_sound_effect(self, sfx_data: Union[list, array.array]):
        if not self._stream or not self._stream.active:
            logging.error("Cannot play SFX: Stream not active.")
            return
        if not sfx_data:
            logging.warning("Empty SFX data provided.")
            return
        with self._sfx_lock:
            self._active_sfx.append((sfx_data, 0, time.time()))


class AudioFile:
    _resampler: Optional[av.AudioResampler]
    _loop: asyncio.AbstractEventLoop = None
    _buffer_samples = None
    container: Optional[av.container.InputContainer] = None
    audio_stream = None
    last_read_pts: Optional[Real] = None
    _container_busy: asyncio.Lock

    def __init__(self, file_path: str, buffer_time=5):
        self.file_path = file_path
        self._fifo = av.AudioFifo()
        self._read_task: Optional[asyncio.Task] = None
        self._not_full = asyncio.Event()
        self._not_full.set()  # Initially, the FIFO is empty, so it's not full
        self._enough_samples = asyncio.Event()  # Set when enough samples are available
        self._enough_samples_num = 0  # Number of samples needed to set the event
        self._read_lock = asyncio.Lock()  # Lock for read operations
        self._eof = False  # End of file flag
        self._buffer_time = buffer_time  # Buffer time in seconds

    async def open(self, *args, resampler: av.AudioResampler = None, **kwargs):
        await self.close()  # Close any existing container before opening a new one
        self._resampler = resampler
        self._loop = asyncio.get_event_loop()
        self.container = await asyncio.to_thread(av.open, self.file_path, *args, **kwargs)
        if (audio_stream_number := len(self.container.streams.audio)) != 1:
            raise ValueError(f"{audio_stream_number} audio streams found in {self.file_path}, expected 1.")
        self.audio_stream = self.container.streams.audio[0]
        self._eof = False
        self._fifo = av.AudioFifo()  # Reset FIFO for new file
        rate = self._resampler.rate if self._resampler else self.audio_stream.rate
        self._buffer_samples = rate * self._buffer_time  # Calculate buffer size in samples
        self._read_task = asyncio.create_task(self._fill_fifo())

    async def _fill_fifo(self):
        try:
            _thread_task = asyncio.create_task(asyncio.to_thread(self.container.decode, self.audio_stream))
            _iter = await asyncio.shield(_thread_task)
            while True:
                # default to None to avoid StopIteration raised in coroutine
                _thread_task = asyncio.create_task(asyncio.to_thread(next, _iter, None))
                raw_frame = await asyncio.shield(_thread_task)

                if raw_frame is None:  # EOF reached, the returned self.close task will clean up
                    if self._resampler is not None:
                        for frame in self._resampler.resample(None):
                            frame.pts = None
                            self._fifo.write(frame)
                    break
                for frame in [raw_frame] if self._resampler is None else self._resampler.resample(raw_frame):
                    frame.pts = None
                    self._fifo.write(frame)

                if self._enough_samples_num:  # feed hungry readers
                    if self._fifo.samples >= self._enough_samples_num:
                        self._enough_samples.set()
                    else:
                        continue  # read() may want more samples than buffer size
                while self._fifo.samples >= self._buffer_samples:
                    self._not_full.clear()
                    await self._not_full.wait()
        except asyncio.CancelledError:
            logging.info("_fill_fifo task was cancelled.")
            if not _thread_task.done():
                logging.debug("Some file operation was performing during cancellation.")
                await _thread_task  # critical file operation in another thread, must finish before closing
            self._fifo.read()
            raise
        finally:
            await self._cleanup()

    def _frame_to_array(self, frame: av.AudioFrame) -> Optional[array.array]:
        if frame is None:
            return
        if frame.pts is None:
            self.last_read_pts = None
        else:
            self.last_read_pts = frame.pts * frame.time_base
        fmt_data = SAMPLE_FMTS_DATA[frame.format.name]
        arr = array.array(fmt_data['type_code'], b'')
        arr.frombytes(memoryview(frame.planes[0])[:frame.samples * frame.format.bytes * frame.layout.nb_channels])
        return arr

    async def read(self, samples: int) -> Optional[array.array]:
        """
        Read a specified number of samples from the audio file.
        Try to return required number of samples and may be fewer if EOF is reached
        :param samples: Positive integer number of samples to read.
        :return: An AudioFrame containing the read samples.
        """
        if self._read_lock.locked():
            raise RuntimeError("Read operation already in progress.")
        if samples <= 0:  # avoid confusion that sample=0 may mean read all
            raise ValueError("Number of samples to read must be positive.")
        for _ in range(2):
            if self._fifo.samples >= samples:
                self._not_full.set()
                return self._frame_to_array(self._fifo.read(samples))
            if self._eof:
                return self._frame_to_array(self._fifo.read())
            logging.debug("Waiting for more samples, current FIFO size: %d, requested: %d", self._fifo.samples, samples)
            self._enough_samples_num = samples
            self._enough_samples.clear()
            async with self._read_lock:  # only lock when waiting for more samples
                await self._enough_samples.wait()
            self._enough_samples_num = 0
        raise RuntimeError("Internal error: failed to read samples after an attempt.")

    def read_nowait(self, samples: int) -> Optional[array.array]:
        try:
            self._loop.call_soon_threadsafe(self._not_full.set)
        except RuntimeError:
            pass
        return self._frame_to_array(self._fifo.read(samples)) if self._fifo.samples >= samples else None

    async def close(self):
        logging.info(f"Closing audio file {self.file_path}")
        if self._read_task is not None and not self._read_task.done():
            logging.debug(f"trying to cancel the fifo filler")
            self._read_task.cancel()
            await asyncio.gather(self._read_task, return_exceptions=True)  # wait but ignore cancellation
        self._fifo.read()
        await self._cleanup()

    async def _cleanup(self):
        self._eof = True
        self._enough_samples.set()  # wake up any waiting readers
        if self.container is not None:
            await asyncio.to_thread(self.container.close)
            self.container = self.audio_stream = None

    def read_thread_safe(self, samples: int):
        return asyncio.run_coroutine_threadsafe(self.read(samples), self._loop)

    @property
    def resampler(self):
        return self._resampler

    @resampler.setter
    def resampler(self, value):
        if self.container is not None:
            raise RuntimeError("Cannot set resampler while container is open.")
        if not isinstance(value, av.AudioResampler):
            raise TypeError("Resampler must be an instance of av.AudioResampler")
        self._resampler = value

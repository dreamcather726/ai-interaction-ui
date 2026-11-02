builtin_interfaces/Time stamp
string request_id
string session_id
string segment_id
string input_context_id
builtin_interfaces/Time capture_started_at
builtin_interfaces/Time capture_ended_at
uint32 sample_rate
uint8 channels
# pcm_s16le, mono, 16000 Hz; up to 300 seconds, recording limit configurable.
string encoding
uint8[<=9600000] data

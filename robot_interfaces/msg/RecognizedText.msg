builtin_interfaces/Time stamp
string request_id
string session_id
string segment_id
string input_context_id
builtin_interfaces/Time capture_started_at
builtin_interfaces/Time capture_ended_at
builtin_interfaces/Time recognized_at
uint8 SUCCESS=0
uint8 NO_SPEECH=1
uint8 FAILED=2
uint8 status
bool success
string message
string error_code
string text
# -1 means unavailable; otherwise [0, 1]. Never bypasses confirmation.
float32 confidence -1.0
string language

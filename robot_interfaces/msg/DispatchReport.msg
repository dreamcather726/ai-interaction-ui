builtin_interfaces/Time stamp
string dispatch_id
string task_id
uint32 revision
uint8 UNKNOWN=0
uint8 ACCEPTED=1
uint8 REJECTED=2
uint8 decision
# True only for ACCEPTED. UNKNOWN is not a known failure.
bool success
string message
string error_code
# Original ExecuteTask request, including rejected dispatches.
string request_id
string session_id
string input_context_id

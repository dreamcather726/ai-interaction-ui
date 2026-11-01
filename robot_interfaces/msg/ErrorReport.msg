# Lifecycle event for a fault; diagnostic measurements use DiagnosticArray.
builtin_interfaces/Time stamp
string source_node
string boot_id
uint64 event_seq
string error_id
string request_id
string task_id
string dispatch_id
uint8 WARN=1
uint8 ERROR=2
uint8 FATAL=3
uint8 severity
bool active
string error_code
string message
bool recovery_required

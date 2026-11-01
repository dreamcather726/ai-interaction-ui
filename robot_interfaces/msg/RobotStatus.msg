# Aggregated business state, not a motion authorization.
builtin_interfaces/Time stamp
string source_node
uint8 UNKNOWN=0
uint8 IDLE=1
uint8 EXECUTING=2
uint8 STOPPING=3
uint8 ESTOP=4
uint8 FAULT=5
uint8 RECOVERING=6
uint8 state
string active_task_id
string active_dispatch_id
string error_code
string message

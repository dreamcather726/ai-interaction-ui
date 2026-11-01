# One component's operational state; measurements use standard ROS messages.
builtin_interfaces/Time stamp
string source_node
string component_id
uint8 UNKNOWN=0
uint8 STARTING=1
uint8 READY=2
uint8 BUSY=3
uint8 DEGRADED=4
uint8 FAULT=5
uint8 OFFLINE=6
uint8 state
bool enabled
string error_code
string message

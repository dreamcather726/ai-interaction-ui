# Liveness only; heartbeat reception does not certify device readiness.
builtin_interfaces/Time stamp
string node_name
string boot_id
uint64 sequence
builtin_interfaces/Duration uptime
builtin_interfaces/Duration expected_period
uint8 UNKNOWN=0
uint8 STARTING=1
uint8 RUNNING=2
uint8 DEGRADED=3
uint8 STOPPING=4
uint8 state
string error_code
string message

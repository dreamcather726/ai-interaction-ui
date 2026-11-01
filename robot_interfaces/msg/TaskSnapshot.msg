builtin_interfaces/Time stamp
robot_interfaces/TaskSpec task
uint32 revision
uint64 event_seq
uint8 PENDING_CONFIRMATION=0
uint8 RUNNING=1
uint8 CANCELING=2
uint8 SUCCEEDED=3
uint8 FAILED=4
uint8 CANCELED=5
uint8 state
string step
builtin_interfaces/Time confirmation_expires_at
string active_dispatch_id
string error_code
string message
bool pickup_completed
bool dropoff_completed
bool return_completed
# Overall measured progress: -1 unknown; otherwise [0, 1].
float32 progress -1.0

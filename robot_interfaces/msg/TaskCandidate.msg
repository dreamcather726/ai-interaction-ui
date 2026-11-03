# Creation time; no execution permission is conveyed by this message.
builtin_interfaces/Time stamp
uint8 UNKNOWN=0
uint8 NAVIGATE=1
uint8 TRANSPORT=2
uint8 SEQUENCE=3
uint8 PICK=4
uint8 PLACE=5
uint8 task_type
string object_id
string pickup_station_id
string dropoff_station_id
string destination_station_id
bool return_required
string return_station_id
# Ordered steps for SEQUENCE. Primitive candidates keep this array empty.
robot_interfaces/TaskStep[] steps

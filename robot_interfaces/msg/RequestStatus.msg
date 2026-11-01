# Business receipt for Topic input; receipt is not task completion.
builtin_interfaces/Time stamp
string request_id
bool success
string message
string error_code
uint8 RECEIVED=0
uint8 REJECTED=1
uint8 stage
# Fully qualified server node and public Topic/Action name.
string source_node
string operation

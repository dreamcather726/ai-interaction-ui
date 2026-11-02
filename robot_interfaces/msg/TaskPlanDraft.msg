# Draft review is not robot execution authorization.
builtin_interfaces/Time stamp
string boot_id
string draft_id
uint32 revision
uint64 event_seq
uint8 EDITING=0
uint8 CONFIRMED=1
uint8 CANCELED=2
uint8 EXPIRED=3
uint8 state
string session_id
string input_context_id
string[] source_request_ids
robot_interfaces/TaskCandidate candidate
string summary
string confirmation_token
builtin_interfaces/Time confirmation_expires_at

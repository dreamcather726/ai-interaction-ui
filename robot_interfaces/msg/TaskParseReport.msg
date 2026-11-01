# 将有效的 ParseTask Result 广播给可视化，不生成执行许可。
builtin_interfaces/Time stamp
string request_id
string session_id
string input_id
string input_context_id
bool success
string message
string error_code
uint8 READY=0
uint8 NEED_CLARIFICATION=1
uint8 UNSUPPORTED=2
uint8 FAILED=3
uint8 CANCELED=4
uint8 outcome
robot_interfaces/TaskCandidate candidate
string[] missing_fields
string clarification_question

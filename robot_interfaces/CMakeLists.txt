cmake_minimum_required(VERSION 3.8)
project(robot_interfaces)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

# find dependencies
find_package(ament_cmake REQUIRED)
find_package(builtin_interfaces REQUIRED)
find_package(std_msgs REQUIRED)
find_package(geometry_msgs REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(nav_msgs REQUIRED)
find_package(action_msgs REQUIRED)
find_package(diagnostic_msgs REQUIRED)
find_package(tf2_msgs REQUIRED)
find_package(rosidl_default_generators REQUIRED)

rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/RequestStatus.msg"
  "msg/AudioSegment.msg"
  "msg/RecognizedText.msg"
  "msg/UserText.msg"
  "msg/DialogueTurn.msg"
  "msg/TaskCandidate.msg"
  "msg/TaskStep.msg"
  "msg/TaskPlanDraft.msg"
  "msg/TaskSpec.msg"
  "msg/TaskDraft.msg"
  "msg/InteractionState.msg"
  "msg/DispatchReport.msg"
  "msg/TaskSnapshot.msg"
  "msg/TaskEvent.msg"
  "msg/ComponentStatus.msg"
  "msg/RobotStatus.msg"
  "msg/MaterialLocation.msg"
  "msg/MaterialState.msg"
  "msg/SafetyState.msg"
  "msg/ErrorReport.msg"
  "msg/NodeHeartbeat.msg"
  "msg/AudioProcessingState.msg"
  "msg/TaskParseReport.msg"
  "srv/PrepareTask.srv"
  "srv/SaveTaskDraft.srv"
  "srv/ReviewTaskDraft.srv"
  "srv/QueryTaskDraft.srv"
  "srv/QueryTask.srv"
  "srv/CancelTask.srv"
  "srv/SetEmergencyStop.srv"
  "srv/ResetEmergencyStop.srv"
  "srv/RecognizeAudio.srv"
  "srv/ProcessAudio.srv"
  "srv/QueryAudio.srv"
  "action/ParseTask.action"
  "action/ExecuteTask.action"
  "action/Speak.action"
  "action/PlayAudio.action"
  DEPENDENCIES builtin_interfaces action_msgs geometry_msgs
)

ament_export_dependencies(rosidl_default_runtime builtin_interfaces action_msgs geometry_msgs)

ament_package()

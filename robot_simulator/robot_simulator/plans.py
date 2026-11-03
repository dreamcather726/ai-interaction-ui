"""Expand the existing candidate contract without adding fields to ExecuteTask."""


def expand_steps(candidate):
    source = (
        candidate.steps if candidate.task_type == candidate.SEQUENCE else [candidate]
    )
    if not source or len(source) > 256:
        raise ValueError("模拟任务必须包含 1～256 个动作")
    result = []
    for step in source:
        kind = step.task_type
        if kind == candidate.TRANSPORT:
            required = (step.object_id, step.pickup_station_id, step.dropoff_station_id)
            actions = [
                ("NAVIGATE", step.pickup_station_id, ""),
                ("PICK", step.pickup_station_id, step.object_id),
                ("NAVIGATE", step.dropoff_station_id, ""),
                ("PLACE", step.dropoff_station_id, step.object_id),
            ]
        elif kind == candidate.NAVIGATE:
            required = (step.destination_station_id,)
            actions = [("NAVIGATE", step.destination_station_id, "")]
        elif kind in (candidate.PICK, candidate.PLACE):
            station = (
                step.pickup_station_id
                if kind == candidate.PICK
                else step.dropoff_station_id
            )
            required = (station, step.object_id)
            actions = [
                ("PICK" if kind == candidate.PICK else "PLACE", station, step.object_id)
            ]
        else:
            raise ValueError(
                "模拟器只接受 NAVIGATE / PICK / PLACE / TRANSPORT / SEQUENCE"
            )
        if any(not item.strip() or len(item) > 256 for item in required):
            raise ValueError("动作工位或物料为空／超长，不能模拟执行")
        result.extend(actions)
        if step.return_required:
            if not step.return_station_id.strip() or len(step.return_station_id) > 256:
                raise ValueError("返回工位无效")
            result.append(("NAVIGATE", step.return_station_id, ""))
        if len(result) > 256:
            raise ValueError("展开后超过 256 个动作，不会截断执行")
    return result


def describe_step(index, total, step):
    kind, station, object_id = step
    action = {"NAVIGATE": "导航到", "PICK": "抓取", "PLACE": "放下"}[kind]
    return f"{index}/{total} {action} {object_id + ' @ ' if object_id else ''}{station}"


def return_step_indices(candidate):
    """Zero-based return-navigation indices when the source explicitly requests them."""
    source = (
        candidate.steps if candidate.task_type == candidate.SEQUENCE else [candidate]
    )
    indices, offset = set(), 0
    for step in source:
        offset += 4 if step.task_type == candidate.TRANSPORT else 1
        if step.return_required:
            indices.add(offset)
            offset += 1
    return indices

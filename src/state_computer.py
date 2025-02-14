# src/state_computer.py

import pandas as pd
from ongoing_process_state.n_gram_index import NGramIndex

class StateComputer:
    """Computes the state of each case using the N-Gram index."""
    def __init__(self, n_gram_index, reachability_graph, event_log_df, bpmn_handler, concurrency_oracle, event_log_ids):
        self.n_gram_index = n_gram_index
        self.reachability_graph = reachability_graph
        self.event_log_df = event_log_df
        self.bpmn_handler = bpmn_handler
        self.concurrency_oracle = concurrency_oracle
        self.event_log_ids = event_log_ids

    def compute_case_states(self):
        """Computes states and active activities for all cases."""
        case_states = {}
        ids = self.event_log_ids  # For convenience
        # Group the event log by CaseId
        grouped = self.event_log_df.groupby(ids.case)
        for case_id, group in grouped:
            # Sort activities by StartTime
            group = group.sort_values(ids.start_time)

            # Identify ongoing activities
            ongoing_activities_df = group[group[ids.end_time].isna()]
            ongoing_activities = []
            for _, row in ongoing_activities_df.iterrows():
                original_activity = row[ids.activity]
                task_id = self.bpmn_handler.get_task_id_by_name(original_activity)
                stime = row[ids.start_time]
                if pd.isna(stime) or task_id is None:
                    enabled_time = None
                else:
                    # Use the original activity name for the concurrency oracle lookup.
                    if original_activity not in getattr(self.concurrency_oracle, 'concurrency', {}):
                        enabled_time = None
                    else:
                        temp_event = pd.Series({
                            ids.activity: original_activity,  # using name here
                            ids.start_time: stime,
                            ids.end_time: stime
                        })
                        enabled_time = row[ids.enabled_time]

                        if pd.isna(enabled_time):
                            print(f"[DEBUG concurrency] Ongoing '{task_id}' in case {case_id}: "
                                  "concurrency oracle returned NaT => no valid enabling event found.")
                ongoing_activities.append({
                    "id": task_id,
                    "start_time": stime,
                    "resource": row[ids.resource],
                    "enabled_time": enabled_time
                })

            # Get the entire sequence of activities (including ongoing ones)
            activities = group[ids.activity].tolist()
            n_gram = [NGramIndex.TRACE_START] + activities
            # Compute the state using N-Gram index
            state_marking = self.n_gram_index.get_best_marking_state_for(n_gram)
            state_flows = state_marking.copy()

            # Get the current marking ID
            current_marking_key = tuple(sorted(state_marking))
            current_marking_id = self.reachability_graph.marking_to_key.get(current_marking_key)
            if current_marking_id is not None:
                for activity in ongoing_activities:
                    t_id = activity['id']
                    # Get incoming edges to the current marking
                    incoming_edges = self.reachability_graph.incoming_edges.get(current_marking_id, [])
                    # Find the edge with the activity label
                    for edge_id in incoming_edges:
                        edge_activity = self.reachability_graph.edge_to_activity.get(edge_id)
                        if edge_activity == t_id:
                            # Get the source marking of that edge
                            source_marking_id, _ = self.reachability_graph.edges[edge_id]
                            source_marking = self.reachability_graph.markings[source_marking_id]
                            # Intersect the source marking with the current state marking
                            state_flows = state_flows.intersection(source_marking)
                            break  # Stop after finding the first matching edge

            # Add names of ongoing activities to the state
            ongoing_activity_ids = set([activity['id'] for activity in ongoing_activities])
            state_activities = ongoing_activity_ids

            # Compute enabled activities
            finished_activities = group[group[ids.end_time].notna()]
            enabled_activities = []
            for flow_id in state_flows:
                target_ref = self.bpmn_handler.sequence_flows.get(flow_id)
                if target_ref in self.bpmn_handler.activities:
                    activity_name = self.bpmn_handler.activities.get(target_ref)
                    if len(finished_activities)== 0:
                        print("reached")
                        enabled_time = min(group[ids.start_time])
                    else:
                        if activity_name not in getattr(self.concurrency_oracle, 'concurrency', {}):
                            self.concurrency_oracle.concurrency[activity_name] = {}
                        max_end_time = finished_activities[ids.end_time].max() if not finished_activities.empty else None
                        temp_event = pd.Series({
                            ids.activity: activity_name,  # using name for lookup
                            ids.start_time: max_end_time + pd.Timedelta(seconds=1),
                            ids.end_time: max_end_time+ pd.Timedelta(seconds=1),
                        })
                        enabled_time = self.concurrency_oracle.enabled_since(trace=finished_activities, event=temp_event)
                    enabled_activities.append({
                        "id": target_ref,
                        "enabled_time": enabled_time
                    })

            # Now compute enabled gateways, skipping those that have upstream tasks still ongoing
            enabled_gateways = []
            for flow_id in state_flows:
                gw_id = self.bpmn_handler.sequence_flows.get(flow_id, None)
                if gw_id and gw_id not in self.bpmn_handler.activities:
                    # Get upstream tasks for this gateway
                    tasks_upstream = self.bpmn_handler.get_upstream_tasks_through_gateways(gw_id)
                    # If any upstream task is still ongoing, skip this gateway
                    if tasks_upstream.intersection(ongoing_activity_ids):
                        continue
                    gw_enabled_time = self._compute_gateway_enabled_time(gw_id, group, finished_activities)
                    if pd.notna(gw_enabled_time):
                        enabled_gateways.append({
                            "id": gw_id,
                            "enabled_time": gw_enabled_time
                        })

            # --- Exclude cases with enabled gateways that are end events ---
            if any(self.bpmn_handler.is_end_event(gateway["id"]) for gateway in enabled_gateways):
                # Skip this case from the process state as it has an enabled gateway that is an end event.
                continue

            # Store case information
            case_states[case_id] = {
                "control_flow_state": {
                    "flows": list(state_flows),
                    "activities": list(state_activities)
                },
                "ongoing_activities": ongoing_activities,
                "enabled_activities": enabled_activities,
                "enabled_gateways": enabled_gateways
            }
        return case_states

    def _compute_gateway_enabled_time(self, gateway_id, group_df, ended_df):
        tasks_upstream = self.bpmn_handler.get_upstream_tasks_through_gateways(gateway_id)
        if not tasks_upstream:
            return ended_df[self.event_log_ids.end_time].max()
        task_names = [
            self.bpmn_handler.activities[t_id]
            for t_id in tasks_upstream
            if t_id in self.bpmn_handler.activities
        ]
        sub_df = ended_df[ended_df[self.event_log_ids.activity].isin(task_names)]
        max_et = sub_df[self.event_log_ids.end_time].max()
        if pd.isna(max_et):
            max_et = ended_df[self.event_log_ids.end_time].max()
        return max_et
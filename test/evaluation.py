# ==============================
# test/evaluation.py
# ==============================

import sys
import os
import json
import math
import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.runner import run_process_state_and_simulation
from src.process_state_prosimos_run import run_basic_simulation
from test import helper

# Log-distance-measures imports
from log_distance_measures.config import (
    AbsoluteTimestampType,
    discretize_to_hour,
    EventLogIDs
)
from log_distance_measures.control_flow_log_distance import control_flow_log_distance
from log_distance_measures.n_gram_distribution import n_gram_distribution_distance
from log_distance_measures.absolute_event_distribution import absolute_event_distribution_distance
from log_distance_measures.case_arrival_distribution import case_arrival_distribution_distance
from log_distance_measures.cycle_time_distribution import cycle_time_distribution_distance
from log_distance_measures.circadian_workforce_distribution import circadian_workforce_distribution_distance
from log_distance_measures.relative_event_distribution import relative_event_distribution_distance


def discard_partial_cases(df: pd.DataFrame, start_dt: pd.Timestamp, horizon_dt: pd.Timestamp) -> pd.DataFrame:
    """
    Discard entire cases if ANY event is partially out of [start_dt, horizon_dt].
    That is, keep a case only if all events lie fully within the time window.
    We'll assume 'start_time' and 'end_time' exist in df.
    """
    if df.empty:
        return df

    keep_case_ids = []
    grouped = df.groupby("case_id", as_index=False)
    for cid, group in grouped:
        min_start = group["start_time"].min()
        max_end   = group["end_time"].max()
        # Condition: entire case is in [start_dt, horizon_dt]
        # i.e. (min_start >= start_dt) AND (max_end <= horizon_dt)
        if (min_start >= start_dt) and (max_end <= horizon_dt):
            keep_case_ids.append(cid)

    return df[df["case_id"].isin(keep_case_ids)].copy()


###########################################################################
# PROCESS-STATE (PARTIAL-STATE) APPROACH
###########################################################################
def evaluate_partial_state_simulation(
    event_log: str,
    bpmn_model: str,
    bpmn_parameters: str,
    start_time: str,
    simulation_horizon: str,
    column_mapping: str,   # JSON string, e.g. '{"case_id":"CaseId","activity":"Activity",...}'
    total_cases: int = 1000,
    sim_stats_csv: str = "sim_stats.csv",
    sim_log_csv: str = "sim_log.csv",
    rename_alog: dict = None,        # how to rename columns in ALog
    rename_glog: dict = None,        # how to rename columns in GLog
    required_columns: list = None,   # columns needed for metrics
    simulate: bool = True,
    verbose: bool = True
) -> dict:
    """
    Runs partial-state simulation (process-state approach) and evaluates distances between the reference event log (ALog)
    and the generated simulation log (GLog), but now excludes partial cases (any event out-of-range => entire case is dropped)
    AND also excludes any case that appears in the partial-state (ongoing/enabled).
    Saves the final DataFrames to CSV so you can see what's actually being compared.
    """
    if required_columns is None:
        required_columns = ["case_id", "activity", "start_time", "end_time", "resource"]

    # 1) Run the partial-state simulation (if requested) with up to 3 retries
    if simulate:
        if verbose:
            print("=== [Process-State] Step 1: Running partial-state simulation ===")
        max_attempts = 3
        attempt = 1
        while attempt <= max_attempts:
            try:
                run_process_state_and_simulation(
                    event_log=event_log,
                    bpmn_model=bpmn_model,
                    bpmn_parameters=bpmn_parameters,
                    start_time=start_time,
                    column_mapping=column_mapping,
                    simulate=True,
                    simulation_horizon=simulation_horizon,
                    total_cases=total_cases,
                    sim_stats_csv=sim_stats_csv,
                    sim_log_csv=sim_log_csv,
                )
                break
            except Exception as e:
                if attempt == max_attempts:
                    raise RuntimeError(f"Process-state simulation failed after {max_attempts} attempts: {e}") from e
                else:
                    print(f"[Process-State] Attempt {attempt} failed: {e}. Retrying...")
                    attempt += 1
    else:
        if verbose:
            print("Skipping simulation (simulate=False).")

    # 2) Read the reference event log (ALog) and the simulation log (GLog)
    if verbose:
        print("=== [Process-State] Step 2: Reading ALog & GLog ===")
    if not os.path.isfile(event_log):
        raise FileNotFoundError(f"ALog file not found: {event_log}")
    if not os.path.isfile(sim_log_csv):
        raise FileNotFoundError(f"GLog file not found: {sim_log_csv}")

    # Load the reference event log
    alog_df = pd.read_csv(event_log)
    # Load the generated simulation log
    glog_df = pd.read_csv(sim_log_csv)

    # 2b) Rename columns if needed
    if rename_alog:
        alog_df.rename(columns=rename_alog, inplace=True)
    if rename_glog:
        glog_df.rename(columns=rename_glog, inplace=True)

    # Convert time columns
    time_cols = ["enable_time", "start_time", "end_time"]
    for col in time_cols:
        if col in alog_df.columns:
            alog_df[col] = pd.to_datetime(alog_df[col], utc=True, errors="coerce")
        if col in glog_df.columns:
            glog_df[col] = pd.to_datetime(glog_df[col], utc=True, errors="coerce")

    # 3) Preprocess logs over the evaluation window
    if verbose:
        print("=== [Process-State] Step 3: Preprocessing logs ===")
    start_dt = pd.to_datetime(start_time, utc=True)
    horizon_dt = pd.to_datetime(simulation_horizon, utc=True)
    A_clean = helper.preprocess_alog(alog_df, start_time=start_dt, horizon=horizon_dt)
    G_clean = helper.preprocess_glog(glog_df, horizon=horizon_dt)

    # 4) Keep only the required columns
    A_clean = A_clean[required_columns]
    G_clean = G_clean[required_columns]

    # 4a) Discard partial cases entirely (any event out-of-range => drop the entire case)
    A_clean = discard_partial_cases(A_clean, start_dt, horizon_dt)
    G_clean = discard_partial_cases(G_clean, start_dt, horizon_dt)

    # 5) Exclude any case that appears in partial-state "output.json" (ongoing or enabled)
    #    We'll assume partial_state was saved as 'output.json'
    #    and that partial_state['cases'].keys() = ongoing/enabled case IDs.
    # partial_state_file = "output.json"
    # partial_state_case_ids = set()
    # if os.path.isfile(partial_state_file):
    #     try:
    #         with open(partial_state_file, "r") as f:
    #             partial_data = json.load(f)
    #             if "cases" in partial_data:
    #                 partial_state_case_ids = set(map(int, partial_data["cases"].keys()))
    #                 if verbose:
    #                     print(f"Excluding {len(partial_state_case_ids)} partial-state cases from ALog & GLog.")
    #     except Exception as e:
    #         if verbose:
    #             print(f"Could not read partial-state from '{partial_state_file}': {e}")

    # if len(partial_state_case_ids) > 0:
    #     A_clean = A_clean[~A_clean["case_id"].isin(partial_state_case_ids)]
    #     G_clean = G_clean[~G_clean["case_id"].isin(partial_state_case_ids)]

    # 6) Save the final "trimmed" logs to CSV for debugging
    if verbose:
        A_clean.to_csv("samples/output/PS_A_clean.csv", index=False)
        G_clean.to_csv("samples/output/PS_G_clean.csv", index=False)
        print("Saved trimmed partial-state logs to 'samples/output/PS_A_clean.csv' and 'PS_G_clean.csv'.")

    # 7) Compute basic stats
    statsA = helper.basic_log_stats(A_clean)
    statsG = helper.basic_log_stats(G_clean)

    # 8) Set up log-distance IDs
    custom_ids = EventLogIDs(
        case="case_id",
        activity="activity",
        start_time="start_time",
        end_time="end_time",
        resource="resource"
    )

    if verbose:
        print("=== [Process-State] Step 5: Computing distances ===")
    distances = {}
    try:
        distances["control_flow_log_distance"] = control_flow_log_distance(A_clean, custom_ids, G_clean, custom_ids)
        distances["n_gram_distribution_distance"] = n_gram_distribution_distance(A_clean, custom_ids, G_clean, custom_ids, n=3)
        distances["absolute_event_distribution_distance"] = absolute_event_distribution_distance(
            A_clean, custom_ids, G_clean, custom_ids,
            discretize_type=AbsoluteTimestampType.START,
            discretize_event=discretize_to_hour
        )
        distances["case_arrival_distribution_distance"] = case_arrival_distribution_distance(
            A_clean, custom_ids, G_clean, custom_ids,
            discretize_event=discretize_to_hour
        )
        distances["cycle_time_distribution_distance"] = cycle_time_distribution_distance(
            A_clean, custom_ids, G_clean, custom_ids,
            bin_size=pd.Timedelta(hours=1)
        )
        distances["circadian_workforce_distribution_distance"] = circadian_workforce_distribution_distance(
            A_clean, custom_ids, G_clean, custom_ids
        )
        distances["relative_event_distribution_distance"] = relative_event_distribution_distance(
            A_clean, custom_ids, G_clean, custom_ids,
            discretize_type=AbsoluteTimestampType.BOTH,
            discretize_event=discretize_to_hour
        )
    except Exception as e:
        raise RuntimeError(f"Error computing distances (process-state): {e}") from e

    result_dict = {
        "ALog_stats": statsA,
        "GLog_stats": statsG,
        "distances": distances,
    }
    if verbose:
        print("[Process-State] Evaluation result:", json.dumps(result_dict, indent=4))
    return result_dict


###########################################################################
# WARM-UP APPROACH
###########################################################################
def evaluate_warmup_simulation(
    event_log: str,
    bpmn_model: str,
    bpmn_parameters: str,
    warmup_start: str,       # warm-up sim begins here
    simulation_cut: str,     # discard events before this date
    simulation_horizon: str, # end date
    total_cases: int = 1000,
    sim_stats_csv: str = "warmup_sim_stats.csv",
    sim_log_csv: str = "warmup_sim_log.csv",
    column_mapping: str = '{"case_id":"CaseId","activity":"Activity","resource":"Resource","start_time":"StartTime","end_time":"EndTime"}',
    rename_alog: dict = None,
    required_columns: list = None,
    simulate: bool = True,
    verbose: bool = True
) -> dict:
    """
    Warm-up simulation approach:
      1) simulate from warmup_start,
      2) discard sim events before simulation_cut
      3) keep events up to simulation_horizon
      4) exclude partial cases (any event out-of-range => entire case removed)
      5) Compare to reference ALog similarly trimmed
      6) Save final CSV so you can see what's compared
    """
    if required_columns is None:
        required_columns = ["case_id", "activity", "start_time", "end_time", "resource"]
    if rename_alog is None:
        rename_alog = {
            "CaseId": "case_id",
            "Activity": "activity",
            "Resource": "resource",
            "StartTime": "start_time",
            "EndTime": "end_time",
        }

    # 1) Possibly run the warm-up sim
    if simulate:
        if verbose:
            print("=== [Warm-up] Step 1: Running basic simulation (warm-up) ===")
        max_attempts = 3
        attempt = 1
        while attempt <= max_attempts:
            try:
                run_basic_simulation(
                    bpmn_model=bpmn_model,
                    json_sim_params=bpmn_parameters,
                    total_cases=total_cases,
                    out_stats_csv_path=sim_stats_csv,
                    out_log_csv_path=sim_log_csv,
                    start_date=warmup_start
                )
                break
            except Exception as e:
                if attempt == max_attempts:
                    raise RuntimeError(f"Warm-up simulation failed after {max_attempts} attempts: {e}") from e
                else:
                    print(f"[Warm-up] Attempt {attempt} failed: {e}. Retrying...")
                    attempt += 1
    else:
        if verbose:
            print("Skipping warm-up simulation (simulate=False).")

    # 2) Load the sim log
    if verbose:
        print("=== [Warm-up] Step 2: Reading simulation log ===")
    if not os.path.isfile(sim_log_csv):
        raise FileNotFoundError(f"Warm-up simulation log not found: {sim_log_csv}")
    sim_df = pd.read_csv(sim_log_csv)
    sim_df.rename(columns=rename_alog, inplace=True)
    for col in ["enable_time", "start_time", "end_time"]:
        if col in sim_df.columns:
            sim_df[col] = pd.to_datetime(sim_df[col], utc=True, errors="coerce")

    # 3) Preprocess the simulation log
    if verbose:
        print("=== [Warm-up] Step 3: Preprocessing simulation log ===")
    start_dt = pd.to_datetime(simulation_cut, utc=True)
    horizon_dt = pd.to_datetime(simulation_horizon, utc=True)
    sim_clean = helper.preprocess_alog(sim_df, start_time=start_dt, horizon=horizon_dt)
    sim_clean = helper.preprocess_glog(sim_clean, horizon=horizon_dt)
    sim_clean = sim_clean[required_columns]

    # 3a) Discard partial cases
    from datetime import datetime
    sim_clean = discard_partial_cases(sim_clean, start_dt, horizon_dt)

    # 3b) Save final sim log for reference
    if verbose:
        sim_clean.to_csv("samples/output/WU_G_clean.csv", index=False)
        print("Saved trimmed warm-up simulation log to 'samples/output/WU_G_clean.csv'.")

    # 4) Read & preprocess reference ALog similarly
    if verbose:
        print("=== [Warm-up] Step 4: Reading and preprocessing reference ALog ===")
    if not os.path.isfile(event_log):
        raise FileNotFoundError(f"Reference event log not found: {event_log}")
    alog_df = pd.read_csv(event_log)
    alog_df.rename(columns=rename_alog, inplace=True)
    for col in ["enable_time", "start_time", "end_time"]:
        if col in alog_df.columns:
            alog_df[col] = pd.to_datetime(alog_df[col], utc=True, errors="coerce")

    A_clean = helper.preprocess_alog(alog_df, start_time=start_dt, horizon=horizon_dt)
    A_clean = helper.preprocess_glog(A_clean, horizon=horizon_dt)
    A_clean = A_clean[required_columns]
    A_clean = discard_partial_cases(A_clean, start_dt, horizon_dt)

    if verbose:
        A_clean.to_csv("samples/output/WU_A_clean.csv", index=False)
        print("Saved trimmed warm-up ALog to 'samples/output/WU_A_clean.csv'.")

    # 5) Basic stats
    if verbose:
        print("=== [Warm-up] Step 5: Computing basic stats on simulation log ===")
    statsSim = helper.basic_log_stats(sim_clean)

    # 6) Distances
    custom_ids = EventLogIDs(
        case="case_id",
        activity="activity",
        start_time="start_time",
        end_time="end_time",
        resource="resource"
    )

    if verbose:
        print("=== [Warm-up] Step 6: Computing distances ===")
    distances = {}
    try:
        distances["control_flow_log_distance"] = control_flow_log_distance(A_clean, custom_ids, sim_clean, custom_ids)
        distances["n_gram_distribution_distance"] = n_gram_distribution_distance(A_clean, custom_ids, sim_clean, custom_ids, n=3)
        distances["absolute_event_distribution_distance"] = absolute_event_distribution_distance(
            A_clean, custom_ids, sim_clean, custom_ids,
            discretize_type=AbsoluteTimestampType.START,
            discretize_event=discretize_to_hour
        )
        distances["case_arrival_distribution_distance"] = case_arrival_distribution_distance(
            A_clean, custom_ids, sim_clean, custom_ids,
            discretize_event=discretize_to_hour
        )
        distances["cycle_time_distribution_distance"] = cycle_time_distribution_distance(
            A_clean, custom_ids, sim_clean, custom_ids,
            bin_size=pd.Timedelta(hours=1)
        )
        distances["circadian_workforce_distribution_distance"] = circadian_workforce_distribution_distance(
            A_clean, custom_ids, sim_clean, custom_ids
        )
        distances["relative_event_distribution_distance"] = relative_event_distribution_distance(
            A_clean, custom_ids, sim_clean, custom_ids,
            discretize_type=AbsoluteTimestampType.BOTH,
            discretize_event=discretize_to_hour
        )
    except Exception as e:
        raise RuntimeError(f"Error computing distances (warm-up): {e}") from e

    result_dict = {
        "SimStats": statsSim,
        "distances": distances,
    }
    if verbose:
        print("[Warm-up] Evaluation result:", json.dumps(result_dict, indent=4))
    return result_dict

"""Agentic replacement for the UiPath "Loan QC Review" automation.

Shape: a goal, a tool registry and an observe -> decide -> act loop. The
decision policy here is deterministic so the sample runs with no model key
and gives repeatable evidence; swap ``policy`` for an LLM-backed planner
(LangGraph, CrewAI, the Claude Agent SDK, ...) and the twin, the scenarios and
the assertions stay exactly the same. That is the point of the twin: the
replacement is judged on the state it leaves behind, not on how it decides.

Set AGENT_BUG=skip_memo to simulate a defective conversion (used by the
negative tests to prove the oracle catches a wrong replacement).
"""
import os

from ats.sdk import Twin

twin = Twin.from_env()
qaqc = twin.http("qaqc")
trace = []


# ---- tools ---------------------------------------------------------------
def tool_get_case(case_id):
    r = qaqc.get(f"/cases/{case_id}")
    return {"status": r.status, "case": r.json() if r.ok else None}


def tool_evaluate_case(case_id):
    r = qaqc.post(f"/cases/{case_id}/evaluate")
    return {"status": r.status, "case": r.json() if r.ok else None, "error": None if r.ok else r.json()}


def tool_post_host_memo(account_id, code):
    cred = twin.credentials("corebank")
    host = twin.terminal("corebank")
    try:
        host.set_field(10, 35, cred["userid"])
        host.set_field(12, 35, cred["password"])
        host.send("ENTER")
        if "MAIN MENU" not in host.text():
            return {"posted": False, "message": host.get(23, 2, 78)}
        host.set_field(20, 15, "2")
        host.send("ENTER")
        host.set_field(5, 25, account_id)
        host.set_field(7, 25, code)
        host.set_field(9, 25, "QC REVIEW")
        host.send("ENTER")
        message = host.get(23, 2, 78)
        host.send("PF3")
        host.set_field(20, 15, "X")
        host.send("ENTER")
        return {"posted": "POSTED" in message, "message": message}
    finally:
        host.close()


def tool_add_note(case_id, text):
    for attempt in range(3):  # same key on every attempt: a lost answer must not duplicate the note
        r = qaqc.post(f"/cases/{case_id}/notes", {"text": text}, headers={"Idempotency-Key": f"note-{case_id}"})
        if r.status == 201:
            return {"status": 201, "attempts": attempt + 1}
    return {"status": r.status, "attempts": 3}


TOOLS = {"get_case": tool_get_case, "evaluate_case": tool_evaluate_case,
         "post_host_memo": tool_post_host_memo, "add_note": tool_add_note}


# ---- policy: what to do next given what has been observed ----------------
def policy(goal, memory):
    case_id = goal["case_id"]
    if "case" not in memory:
        return "get_case", {"case_id": case_id}
    if memory["case"]["status"] == 404:
        return "finish", {"status": "NOT_FOUND"}
    first = memory["case"]["case"]
    if first["state"] not in ("NEW", "PENDING_EVIDENCE"):
        return "finish", {"status": "SKIPPED", "final_state": first["state"]}
    if "evaluation" not in memory:
        return "evaluate_case", {"case_id": case_id}
    if memory["evaluation"]["status"] != 200:
        return "finish", {"status": "FAILED"}
    case = memory["evaluation"]["case"]
    memo = {"COMPLETED": "QCPASS", "EXCEPTION": "QCHOLD"}.get(case["state"], "")
    if memo and os.environ.get("AGENT_BUG") == "skip_memo":
        memory.setdefault("memo", {"posted": True, "message": "(skipped by injected defect)"})
    if memo and "memo" not in memory:
        return "post_host_memo", {"account_id": case["account_id"], "code": memo}
    host_result = "NONE" if not memo else memo if memory["memo"]["posted"] else "REJECTED"
    if "note" not in memory:
        return "add_note", {"case_id": case_id, "text": f"QC result {case['state']}; host memo {host_result}"}
    if memory["note"]["status"] != 201:
        raise SystemExit("case note was not accepted")
    return "finish", {"status": "HOST_REJECTED" if host_result == "REJECTED" else "DONE",
                      "final_state": case["state"], "memo_code": memo if host_result == memo else ""}


SLOT = {"get_case": "case", "evaluate_case": "evaluation", "post_host_memo": "memo", "add_note": "note"}


def main():
    goal = {"case_id": twin.inputs["case_id"]}
    memory = {}
    for step in range(1, 12):
        action, args = policy(goal, memory)
        if action == "finish":
            print(f"step {step}: finish {args}")
            twin.finish({"status": args["status"], "final_state": args.get("final_state"),
                         "memo_code": args.get("memo_code"), "trace": trace})
            return
        observation = TOOLS[action](**args)
        memory[SLOT[action]] = observation
        trace.append({"step": step, "tool": action, "args": args})
        print(f"step {step}: {action}({args}) -> {str(observation)[:120]}")
    raise SystemExit("agent did not reach a final answer within its step budget")


if __name__ == "__main__":
    main()

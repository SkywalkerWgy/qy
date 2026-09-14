from loop import Loop

sys_prompt = """
You are an expert in ACSL-based deductive program verification (Frama-C).  
Your job is to generate **only loop invariants** (ACSL `loop invariant` clauses), depending on the task type:

1. **Obligation-Guided Initial Invariant Inference**
   - Insert initial invariant candidates at the marked hint.
   - Use the given reachability and proof-obligation contexts.
   - Output only ACSL `loop invariant` clauses.

2. **Failure Diagnosis**
   - Diagnose why a verification obligation failed.
   - Do not propose invariants in diagnosis responses.
   - Output only the requested diagnosis fields.

3. **Dependency-Guided Refinement**
   - Refine only the loop invariants in the requested repair scope.
   - Output one block per loop in scope, containing only new or modified invariant clauses.

RULES:  
- Follow the requested output format exactly.  
- **Never restate original invariants unless modifying them**.  
- **All invariants must satisfy establishment, preservation, and zero-iteration validity**.  
- Use ACSL syntax: &&, ||, ==> , \\forall, \\exists, etc.  
"""

prompt_template_obligation_guided_initial = """
<The code I give you>
You are an expert in program verification. Please generate initial loop invariant candidates as C annotation comments at the hint location \
(annotated by "[Hint] Please infer the loop invariant for the following loop.") using ACSL language strictly based on the program's execution logic and standard correctness..
ACSL is a specification language for C programs that conforms to the design by contract paradigm, utilizing Hoare style pre- and postconditions and invariants.

Specifically, for the current verification task:
    - { <ASSUMPTION_CONTEXT> } <PRE_STATE> { I }:
      after the pre-state transition, the assumption context should establish the generated invariant.
<OBLIGATION_TRIPLES>

Please focus on invariants justified by the reachability and provability contexts above, and verify that the generated loop invariant is consistent with these proof contexts.
Preservation is not the current focus and will be handled in later steps.
Use '&&', '||', '==>', '\\forall' or '\\exists' if necessary.
Your answer should follow the following format:
```
loop invariant ...;
loop invariant ...;
...
```
No explanation. No commentary. Just show me the loop invariant.
"""

prompt_template_failure_diagnosis = """
<The code I give you>
You are an expert in program verification using ACSL and Frama-C. Diagnose why the following verification obligation cannot be discharged. Do not propose new invariants.
[Failed Invariant] <ERROR_INV>
[Failed Property] <FAILED_PROPERTY>
[WP Target] <WP_GOAL>
[Available Assumptions] <INNER_INV> and ! (<INNER_LOOP_CONDITION>)
Analyze the proof failure by answering the following items:
    - Which proof property failed and what that property requires.
    - Whether failed invariant itself is too strong or non-inductive with respect to the loop body.
    - Whether the invariant(s) of <INNER_LOOP> are too weak to imply the WP goal after the inner loop terminates.
    - Which missing logical fact is needed to bridge the available assumptions and WP target.

IMPORTANT RULES:
1. Output only a diagnosis object using the following fields:
```
[Failed Property]
...
[Failure Cause]
...
[Responsible Scope]
...
[Refinement Direction]
...
```
2. The [Failed Property] field must copy the input [Failed Property] exactly; it must name the proof property that failed, not restate the invariant formula.
3. The responsible scope must be selected from <CUR_LOOP>, <INNER_LOOP>, or both.
4. Boundary contracts may be cited as assumptions or targets, but must not be selected as repair targets.
"""

prompt_template_initial_reasoning_ablation = """
<The code I give you>
You are an expert in program verification, and please generate loop invariant as C annotation comments at the hint location \
(annotated by "[Hint] Please infer the loop invariant for the following loop.") using ACSL language.
Your answer should follow the following format:
```
loop invariant ...;
loop invariant ...;
...
```
No explanation. No commentary. Just show me the loop invariant.
"""

prompt_template_dependency_refinement = """
<The code I give you>
You are an expert in program verification using ACSL and Frama-C. Refine only the loop invariants in the repair scope so that the failed verification obligation can be discharged.
[Failed Invariant] <ERROR_INV>
[Failed Property] <FAILED_PROPERTY>
[Failure Cause] <FAILURE_CAUSE>
[Repair Scope] <RESPONSIBLE_SCOPE>
[Refinement Direction] <REFINEMENT_DIRECTION>
Use the failed property and diagnosis to refine the failed invariant and its related invariants.
IMPORTANT RULES:
1. Modify only invariants in <RESPONSIBLE_SCOPE>.
2. Your answer should follow the following format:
```
<LOOP_IN_SCOPE>
loop invariant ...;
...
```
Output one block for each loop in the repair scope. All invariants must strictly satisfy ACSL rules.
3. Do not output any of the original invariants. Only output the new or modified invariants you propose.
4. No explanation. No commentary. Just show me the loop invariant.
"""

prompt_template_batch_feedback_refinement_ablation = """
<The code I give you>
You are an expert in ACSL-based deductive program verification with Frama-C.
The loop invariants already generated in the code are not sufficient to prove the program.
For this ablation setting, keep the existing loop invariants in place and refine the proof by adding only the new invariant clauses needed to address the failed verification obligations.

Here are all failed proof properties reported by Frama-C/WP in the last verification attempt:
<ERROR_MSG_HERE>

Please refine the invariants for the relevant loop(s). Your answer must follow this exact block format:
```
[Loop <ID>]
loop invariant ...;
loop invariant ...;

[Loop <ID>]
loop invariant ...;
loop invariant ...;
```
Use the loop IDs shown in the code, such as A, B, C, or their full labels.
Do not repeat existing invariants unless a logically stronger supporting fact is needed.
Do not explain the errors. Do not include assertions, lemmas, code, or comments.
No explanation. No commentary. Just show me the loop invariant.
"""

def get_prompt(prompt_code, prompt_type: str, current_loop: Loop = None, error_msg = None, inner_loop: Loop = None, error_wp: str = "", assumption_context: str = "", pre_state: str = "", obligation_triples: str = "", inner_loop_invariants: str = "", inner_loop_condition: str = "", failure_property: str = "", failure_cause: str = "", responsible_scope: str = "", refinement_direction: str = "", loop_in_scope: str = ""):
    if prompt_type == "obligation_guided_initial":
        question = (
            prompt_template_obligation_guided_initial
            .replace("<The code I give you>", prompt_code)
            .replace("<ASSUMPTION_CONTEXT>", assumption_context)
            .replace("<PRE_STATE>", pre_state)
            .replace("<OBLIGATION_TRIPLES>", obligation_triples)
        )
    elif prompt_type == "initial_reasoning_ablation":
        question = prompt_template_initial_reasoning_ablation.replace("<The code I give you>", prompt_code)
    elif prompt_type == "failure_diagnosis":
        inner_loop_name = inner_loop.display_name if inner_loop else "Boundary"
        question = (
            prompt_template_failure_diagnosis
            .replace("<The code I give you>", prompt_code)
            .replace("<ERROR_INV>", error_msg or "")
            .replace("<FAILED_PROPERTY>", failure_property or "")
            .replace("<WP_GOAL>", error_wp or "")
            .replace("<INNER_INV>", inner_loop_invariants or "true")
            .replace("<INNER_LOOP_CONDITION>", inner_loop_condition or "true")
            .replace("<CUR_LOOP>", current_loop.display_name if current_loop else "CurrentLoop")
            .replace("<INNER_LOOP>", inner_loop_name)
        )
    elif prompt_type == "dependency_refinement":
        question = (
            prompt_template_dependency_refinement
            .replace("<The code I give you>", prompt_code)
            .replace("<ERROR_INV>", error_msg or "")
            .replace("<FAILED_PROPERTY>", failure_property or "")
            .replace("<FAILURE_CAUSE>", failure_cause or "")
            .replace("<RESPONSIBLE_SCOPE>", responsible_scope or "")
            .replace("<REFINEMENT_DIRECTION>", refinement_direction or "")
            .replace("<LOOP_IN_SCOPE>", loop_in_scope or "")
        )
    elif prompt_type == "batch_feedback_refinement_ablation":
        question = (
            prompt_template_batch_feedback_refinement_ablation
            .replace("<The code I give you>", prompt_code)
            .replace("<ERROR_MSG_HERE>", error_msg or "")
        )
    else:
        question = ""
    return question


if __name__ == "__main__":
    with open("../Benchmark/acsl-algorithms/_edit_distance.cbs", "r", encoding="utf-8") as f:
        c = f.read()
    print(get_prompt(c, "obligation_guided_initial"))

# Evidence Discipline

## FACT / INFERENCE / HYPOTHESIS

Label claims by how they were established, and keep the label attached as
the claim gets reused:

- **FACT** — directly observed just now (a command you ran, a file you
  read, a response you received) with the evidence still in hand.
- **INFERENCE** — a conclusion drawn from facts, but not itself directly
  observed (e.g. "the service is probably using this code" because the
  deploy log says so, without having read the running process's actual
  loaded module).
- **HYPOTHESIS** — an untested guess offered to explain something, not yet
  checked against evidence either way.

An active hypothesis must not be reported, or later treated, as a fact
without being explicitly confirmed against direct evidence. A hypothesis
that gets disproven must not keep being cited as if it still held — retract
it as soon as evidence contradicts it, don't let it linger.

## Provenance

- State where a piece of evidence came from: local disk, GitHub (which
  ref/SHA), or the VPS (which log, which command, when).
- These three are not interchangeable. Local main, origin/main, and the
  code actually running on the VPS can all differ at the same moment — say
  which one a claim is about.
- Code sitting on disk is not the same fact as code a running process has
  loaded. A service can be running stale code after a file changed but
  before a restart; don't conflate "the file says X" with "the process is
  doing X" unless you've confirmed the process was restarted/reloaded since.

## Logs and time

- Time-bound every claim based on logs: say what time range they cover and
  when they were pulled. Old logs describe past state, not current state —
  don't present them as a live picture of what's happening now.
- A pushed branch or merged PR is not automatically deployed. Deployment is
  a separate, verifiable event — check for it rather than assuming it
  follows from a push or merge.

## Reporting

- If evidence is incomplete or a check couldn't be performed, say so
  explicitly rather than filling the gap with an assumption. "I don't know
  because I couldn't check X" is a valid and expected answer.

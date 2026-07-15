# RadioTEDU Windows broadcast services

This package installs two Windows services:

- `RadioTEDU.SharedAI` owns loopback-only Ollama and Qwen TTS.
- `RadioTEDU.BroadcastSupervisor` starts, monitors, and independently recovers the EN and FR station child processes. The same supervisor process owns the single outbound-only `PublicSyncService` and durable outbox.

Create the two protected environment files in `C:\ProgramData\RadioTEDU\config` from the examples. Restrict their ACLs to the service identity and administrators; never put source credentials or HMAC secrets in the repository, prompt, command history, or logs.

Run `install-services.ps1` from elevated PowerShell. The installer creates services without starting them unless `-Start` is supplied. Use `-Start` only on the target broadcast computer after staging preflight, FDK-AAC verification, separate `/en` and `/fr` mount checks, and API conformance tests pass.

Both stations intentionally share the Icecast host at `10.98.98.75:11154`. Station processes, queues, databases, and recovery remain isolated, but the Icecast host is one acknowledged failure domain.

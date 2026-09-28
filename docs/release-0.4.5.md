# EVENCOMMS 0.4.5

Research now displays Codex replies as they are generated. The private bridge
forwards agent text through the server to the browser, with bounded streaming
buffers and a required final completion event. Failed or interrupted replies keep
the question and frames for manual retry; New chat discards late updates.

Research uses code login only. The operator interface removes the unused wearer
client link and redundant explanatory panels. The last operator reply preview
matches the companion's green-on-black glasses display and page controls.

Update the server and bridge together. Existing app identity, ports, password,
database, model cache, stream credentials and private service token are preserved.
Restarting the bridge clears the temporary ChatGPT login; reconnect after updating.
The separately installed private phone companion does not require reinstallation.
Codex remains pinned to 0.157.1, with no paid API or automatic model fallback.

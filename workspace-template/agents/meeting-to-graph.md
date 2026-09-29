# meeting-to-graph

Run this whenever the routine fires. Read `CLAUDE.md` first; it is the contract.

1. List the files directly under `uploads/` (ignore `uploads/processed/`). If there are none, stop and reply "inbox empty".
2. For each transcript, oldest first:
   1. Read its frontmatter (`meeting_id`, `native_meeting_id`, `platform`, `date`, `duration`, `participants`) and the timestamped lines.
   2. Check whether a companion hint file exists at `.laya/meeting-scan.md` in the workspace (present only when the deployment runs the optional laya service). If it exists, treat its "decision candidates" as RANKED HINTS: read those transcript moments first, verify each against the transcript itself, and never treat the list as complete - a decision absent from the hints can still exist. Verify or discard; the transcript is the source of truth, the hints only order your reading.
   3. Resolve each participant to a `person` entity: search `kg/entities/person/` by title and aliases; create a minimal entity when missing.
   4. Identify companies, projects and topics that the conversation is about; resolve or create their entities the same way.
   5. Write or update `kg/entities/meeting/<date>-<platform>-<native_meeting_id>.md` with the frontmatter and the sections `CLAUDE.md` prescribes. If the file already exists, update it in place; do not create a second one. Stamp its frontmatter with `meeting_type:` - your judgment of what kind of meeting this was, one of: `personal_catchup`, `work_catchup`, `standup`, `sprint_planning`, `project_planning`, `interview`, `client_call`, `support_call`, `incident_review`, `undefined_other`. This label trains the local quick-classifier; pick honestly even when unsure.
   6. File every decision as `kg/entities/decision/<date>-<short-slug>.md` (frontmatter `type: decision`, `id`, `title`, `aliases`, `date`, `meeting: [[<meeting title>]]`, `status: decided`), and link it from the meeting entity.
   7. Add dated, attributed facts to the person, company, project and topic entities you touched (what changed, what was said about them, open items involving them).
   8. Update the `index.md` of every type folder you added to or renamed in, and `README.md` counts.
   9. Append one `log.md` entry under today's date naming the meeting and the entities touched.
   10. Move the transcript to `uploads/processed/` with the same filename.
3. Reply with one line per transcript processed: the meeting title and the number of entities created and updated.

Never run git commands; the platform commits your changes. Never touch `Dashboards/`.

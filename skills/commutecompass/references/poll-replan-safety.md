# Poll replan safety

Alert and Home Assistant replans retain the larger persisted realtime buffer
when the scheduled journey is unchanged, even when Directions returns no GTFS
identity or a smaller observation. Equality compares destination kind/value/
coordinates, route departure/arrival/duration, and the ordered legs' mode,
system, line, headsign, departure/arrival/duration and boarding/alighting stop
names. Provider payload, GTFS metadata, timing-trust flags and display summaries
are not journey identity. GTFS identity is never copied between plans. The only
producer is the static-schedule bridge (`gtfs_bridge.validate_leg`, called from
routing on the first transit leg of an allowlisted subway journey). It sets the
identity fields plus `gtfs_provenance` only when exactly one static stop event
matches within ±30 s. Everything else, including cached/estimated routes and any
bridge failure, stays unmatched for realtime observation and gets zero automatic
padding.

Only the missing padding delta is subtracted, in UTC; newly computed weather
and travel timing remain intact. The persisted prep-to-leave interval is kept
(including manual prep adjustments). A different journey keeps its own realtime
buffer/reason rather than reusing a previous trip's reason.

Significant replans commit with `BEGIN IMMEDIATE` and full-plan equality against
the snapshot taken before network work. Stale results do not write plans or
pings or send update notifications; stale alert results remain unseen for a
future poll. Existing significance thresholds and announcement dedup remain.
Alert and Home Assistant replans skip events whose start is at or before the
live poll time. Inside the write transaction, both the current and replanned
event starts must still be strictly in the future (compared as UTC instants),
including when an event starts during network work. Ineligible results cannot
update the plan, create actionable alarms, or send an update notification.

Plan and pending actionable updates commit together. Fired rows remain fired;
existing IDs, retry counts and pending offsets (including snoozes) survive.
Already-due alarms cannot be postponed. Newly due alarms are clamped to the
poll's captured time and remain available for the next dispatch, never dropped.
An absent actionable kind is created only if no row of that kind already exists
and the event is still upcoming. Past prep/leave times for an upcoming event
remain eligible for immediate alarms; this is not a blanket past-alarm filter.

Poll claims the current payload using a live clock. When both travel reminders
are due, leave supersedes prep. Poll retires an obsolete prep row inside the
claim transaction, using the current plan and leave rows rather than the pending
snapshot. A due or already-consumed leave, or a `too_imminent` plan, makes prep
obsolete. Presence deferral and quiet-hours eligibility run first, so suppression
does not consume a row that should remain deferred. A genuinely future departure
still permits a due prep reminder.

Location replans decide whether an actionable reminder is due inside their
reconciliation transaction. When one is due, poll uses the normal atomic dispatch
path instead of sending a redundant location update. Newly urgent reminders are
dispatched in the same invocation; claimed IDs are tracked across dispatch passes
so a failed send cannot immediately retry in that invocation. An already-consumed
imminent departure does not generate another generic “leave now” location update.
Future location changes and distinct MTA service updates retain their normal
announcement behavior. No network send occurs inside a database transaction.

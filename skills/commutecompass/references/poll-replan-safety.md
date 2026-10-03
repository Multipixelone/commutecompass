# Poll replan safety

Alert and Home Assistant replans retain the larger persisted realtime buffer
when the scheduled journey is unchanged, even when Directions returns no GTFS
identity or a smaller observation. Equality compares destination kind/value/
coordinates, route departure/arrival/duration, and the ordered legs' mode,
system, line, headsign, departure/arrival/duration and boarding/alighting stop
names. Provider payload, GTFS metadata, timing-trust flags and display summaries
are not journey identity. No GTFS identity is inferred or copied to Directions.
Ordinary Directions routes remain unmatched for realtime observation.

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

Claim-current-payload and live-clock refinements are separate follow-ups.

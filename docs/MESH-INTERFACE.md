# Meshtastic interface

Outpost's Meshtastic direct-message interface is a primary product surface. It exposes the same
community, safety, environmental, location, mail, and local-assistant capabilities as the web
application without requiring members to learn a command language.

## Start here

Open a direct message to the Outpost node and send `?`:

```text
OUTPOST / HOME
1 Weather & alerts
2 Incidents & safety
3 Community boards
4 Mail
5 People & places
6 Ask Outpost
7 My account
8 Command shortcuts
0 Home · ? Menu
```

The choices reflect the modules enabled on that Outpost and the member's trust level. A guest may
therefore see fewer choices than a named member or responder.

## Interaction rules

- Reply with the displayed number. Labels and common words such as `weather`, `inbox`, and `cancel`
  are accepted where they are unambiguous.
- When a screen asks for text, send only the requested value. For example, the mail composer first
  asks whom to contact and then asks for the message.
- `0` returns to Home. `?`, `MENU`, and `HOME` rebuild navigation even when a previous response was
  lost.
- A command such as `WX`, `WARN`, `BOARDS`, `MAIL`, `REPORT`, or `PING` works at any screen and
  cancels an unfinished prompt. This makes screen state an accelerator, never a prerequisite.
- Command names are case-insensitive. Common phrases and minor command typos are resolved before
  the local AI is considered.
- A bare number without an active screen is never guessed. Outpost asks the member to send `?`.

## Guided journeys

Create a community post:

```text
?  -> Community boards -> Create a post
1  -> choose the displayed board
Bridge open one lane near Mill Road.
```

Send stored mail:

```text
?  -> Mail -> Send a message
1  -> choose a recently heard member
Check the north entrance.
```

Report a community incident:

```text
?  -> Incidents & safety -> Report a problem
Tree blocks Oak Street near the school.
```

Reports and welfare check-ins are community coordination records, not emergency calls. Mail is
hidden from other members but stored plaintext is readable by the Outpost operator.

## Airtime behavior

Fixed discovery screens are kept within one 200-byte application payload. Dynamic content may be
paged, and detailed results may use bounded multipart replies. Navigation has a separate abuse
limit so exploring the interface does not consume the small ordinary-command allowance; all
traffic still passes through the node-wide airtime governor.

Public and secondary channels do not run the stateful interface. They require the configured
prefix and keep replies terse; direct-message the node for guided navigation. Power users can use
the complete [mesh command reference](COMMANDS.md) at any time.

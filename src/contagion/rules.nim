## Contagion action parsing and timeout fallback. Game and players share
## public rules; model transport and prompting live in the player process.

import std/[json, strutils, unicode]
import sim

type
  ScriptKind* = enum
    skNone = "none"
    skSentinel = "sentinel"
    skLaggard = "laggard"

proc parseScriptKind*(text: string): ScriptKind =
  ## PLAYER_SCRIPTED values: empty selects the ordinary model player;
  ## "sentinel" or "laggard" selects an explicit scripted baseline.
  let cleaned = text.strip().toLowerAscii()
  case cleaned
  of "": skNone
  of "sentinel": skSentinel
  of "laggard": skLaggard
  else:
    raise newException(ValueError,
      "unknown PLAYER_SCRIPTED value: " & text & " (expected sentinel or laggard)")

# ---- Scripted baselines -----------------------------------------------------
#
# Both baselines see ONLY what a seat sees — reported cases and published
# dials, never the true infection counts — are deterministic, are always
# legal, and never talk, take notes or send aid.

proc estimatedCases*(region: RegionState): int64 =
  ## A region's reported cases de-biased by its PUBLISHED testing level. Every
  ## governor can compute this for every region; that inference is intended
  ## play, not a leak.
  region.confirmed * Ppm div DetectPpm[region.testing]

proc estimatedRatePpm*(region: RegionState): int64 =
  let living = region.alive
  if living <= 0: 0 else: estimatedCases(region) * Ppm div living

const
  SentinelLockdownCutsPpm* = [1_000'i64, 4_000, 12_500, 30_000]
    ## Own de-biased prevalence, in ppm, at which the sentinel steps its
    ## lockdown 0->1, 1->2, 2->3, 3->4.
  SentinelTestingCutsPpm* = [1_000'i64, 12_500]
    ## ... and its testing 1->2, 2->3.
  SentinelRoadCutsPpm* = [160'i64, 800]
    ## A NEIGHBOUR's de-biased prevalence, in ppm, at which the sentinel
    ## screens (gate 1) and then closes (gate 2) the road to it.
    ##
    ## All three families are the argmax of the x0.25..x4 grid swept in
    ## tests/test_sweep.nim over five seeds; that test re-runs the sweep and
    ## fails if these stop being its best cell. They are NOT hand-picked.

proc sentinelDecision*(sim: Sim, seat: int): Decision =
  ## The threshold dial policy, and the universal fallback move.
  let pos = sim.posOf[seat]
  ## Once the variant is confirmed every threshold tightens by 20%, so the
  ## sentinel reacts one step earlier.
  let scale = if sim.variantActive(): 800_000'i64 else: Ppm
  let own = estimatedRatePpm(sim.regions[pos])
  result = blankDecision()
  result.lockdown =
    if own < SentinelLockdownCutsPpm[0] * scale div Ppm: 0
    elif own < SentinelLockdownCutsPpm[1] * scale div Ppm: 1
    elif own < SentinelLockdownCutsPpm[2] * scale div Ppm: 2
    elif own < SentinelLockdownCutsPpm[3] * scale div Ppm: 3
    else: 4
  result.testing =
    if own < SentinelTestingCutsPpm[0] * scale div Ppm: 1
    elif own < SentinelTestingCutsPpm[1] * scale div Ppm: 2
    else: 3
  for slot in 0 ..< Degree:
    let far = otherEnd(NeighboursOf[pos][slot], pos)
    let theirs = estimatedRatePpm(sim.regions[far])
    result.borders[slot] =
      if theirs < SentinelRoadCutsPpm[0] * scale div Ppm: 0
      elif theirs < SentinelRoadCutsPpm[1] * scale div Ppm: 1
      else: 2

proc laggardDecision*(sim: Sim, seat: int): Decision =
  ## The leaky neighbour: blind (testing 0, so it under-reports by 6.7x and
  ## never isolates), wide open, and late — lockdown 3 for three weeks once
  ## its own de-biased estimate first crosses 4%, then open again forever.
  let pos = sim.posOf[seat]
  result = blankDecision()
  result.lockdown = 0
  result.testing = 0
  result.borders = [0, 0, 0]
  var trigger = -1
  for week, record in sim.history:
    if estimatedRatePpm(record.regions[pos]) >= 40_000:
      trigger = week
      break
  if trigger >= 0 and sim.week >= trigger and sim.week < trigger + 3:
    result.lockdown = 3

proc scriptedDecision*(sim: Sim, seat: int, kind: ScriptKind): Decision =
  ## Rule-based baseline for `seat`. Always legal; never talks, notes or aids.
  case kind
  of skLaggard: laggardDecision(sim, seat)
  else: sentinelDecision(sim, seat)

proc decisionJson*(sim: Sim, seat: int, decision: Decision): JsonNode =
  ## Complete player action for the seat's three named roads.
  let pos = sim.posOf[seat]
  var borders = newJObject()
  for slot in 0 ..< Degree:
    let neighbour = otherEnd(NeighboursOf[pos][slot], pos)
    borders[RegionNames[neighbour]] = %decision.borders[slot]
  var aid = newJArray()
  for transfer in decision.aid:
    aid.add(%*{"to": RegionNames[transfer.to], "amount": transfer.amount})
  result = %*{
    "lockdown": decision.lockdown,
    "testing": decision.testing,
    "borders": borders,
    "aid": aid,
    "say": decision.say,
    "notes": decision.notes
  }

proc cleanText*(text: string, limit: int): string =
  ## Text over the cap is cut at a RUNE boundary with the cut marked. A byte
  ## slice through a multi-byte character would leave invalid UTF-8 in the
  ## replay and break its JSON.
  result = text.strip()
  if result.runeLen <= limit:
    return
  result = result.runeSubStr(0, limit - 1) & "…"

proc parseReplyJson*(reply: string): JsonNode =
  ## Parses a raw JSON object or a single standard outer Markdown JSON fence.
  var body = reply.strip()
  if body.startsWith("```"):
    let nl = body.find('\n')
    if nl < 0 or not body.endsWith("```"):
      raise newException(ContagionError, "unterminated markdown code fence")
    let fenceLang = body[3 ..< nl].strip().toLowerAscii()
    if fenceLang.len > 0 and fenceLang != "json":
      raise newException(ContagionError,
        "unsupported markdown code fence language: " & fenceLang)
    body = body[nl + 1 ..< body.len - 3].strip()
  if body.len == 0:
    raise newException(ContagionError, "empty action reply")
  try:
    result = parseJson(body)
  except CatchableError as error:
    raise newException(ContagionError, "invalid JSON action: " & error.msg)
  if result.kind != JObject:
    raise newException(ContagionError, "action reply must be a JSON object")

proc parseDialInt(node: JsonNode, name: string, lo, hi: int): int =
  ## Strict integer dial parser. Missing, non-integer, or out-of-range raises.
  if node.isNil or node.kind == JNull:
    raise newException(ContagionError, "no " & name & " in response")
  if node.kind != JInt:
    raise newException(ContagionError, name & " must be an integer: " & $node)
  let value = node.getInt()
  if value < lo or value > hi:
    raise newException(ContagionError,
      name & " must be " & $lo & ".." & $hi & ": " & $value)
  value

proc parseDecision*(sim: Sim, seat: int, payload: JsonNode): Decision =
  ## Strict canonical decision parser. Optional fields (`borders`, `aid`, `say`,
  ## `notes`) default cleanly; invalid fields or out-of-range values raise
  ## ContagionError instead of being silently corrected.
  if payload.isNil or payload.kind != JObject:
    raise newException(ContagionError, "decision action must be a JSON object")
  let pos = sim.posOf[seat]
  result = blankDecision()
  let notesNode = payload{"notes"}
  if not (notesNode.isNil or notesNode.kind == JNull):
    if notesNode.kind != JString:
      raise newException(ContagionError, "notes must be a string")
    result.notes = cleanText(notesNode.getStr(), MaxNotesLen)
  let sayNode = payload{"say"}
  if not (sayNode.isNil or sayNode.kind == JNull):
    if sayNode.kind != JString:
      raise newException(ContagionError, "say must be a string")
    result.say = cleanText(sayNode.getStr(), MaxSayLen).replace("\n", " ")
  result.lockdown = parseDialInt(payload{"lockdown"}, "lockdown", 0, 4)
  result.testing = parseDialInt(payload{"testing"}, "testing", 0, 3)

  let borders = payload{"borders"}
  if not (borders.isNil or borders.kind == JNull):
    if borders.kind != JObject:
      raise newException(ContagionError, "borders must be a JSON object")
    for key, value in borders:
      let far = positionOfName(key)
      var slot = -1
      if far >= 0:
        for candidate in 0 ..< Degree:
          if otherEnd(NeighboursOf[pos][candidate], pos) == far:
            slot = candidate
      if slot < 0:
        raise newException(ContagionError,
          "unknown or non-adjacent border road: " & key)
      result.borders[slot] = parseDialInt(value, "gate", 0, 2)

  let aid = payload{"aid"}
  if not (aid.isNil or aid.kind == JNull):
    if aid.kind != JArray:
      raise newException(ContagionError, "aid must be a JSON array")
    if aid.len > MaxAidEntries:
      raise newException(ContagionError,
        "aid may contain at most " & $MaxAidEntries & " entries")
    for entry in aid:
      if entry.kind != JObject:
        raise newException(ContagionError, "aid entry must be a JSON object")
      let toNode = entry{"to"}
      if toNode.isNil or toNode.kind != JString:
        raise newException(ContagionError, "aid.to must be a region name string")
      let name = toNode.getStr()
      let far = positionOfName(name)
      if far < 0 or far == pos:
        raise newException(ContagionError, "invalid aid recipient: " & name)
      let amountNode = entry{"amount"}
      if amountNode.isNil or amountNode.kind != JInt:
        raise newException(ContagionError, "aid.amount must be an integer")
      let amount = amountNode.getBiggestInt()
      if amount < 0:
        raise newException(ContagionError, "aid.amount must be nonnegative")
      if amount == 0:
        continue
      result.aid.add(AidEntry(to: far, amount: amount))

--[[
  GT06 / W07C GPS tracker protocol dissector for Wireshark (Lua).

  Decodes the TCP protocol spoken by GT06-family trackers (incl. the W07C units
  used by windsurfer-tracker) on port 7711: login, location, heartbeat, alarm/SOS,
  and the ASCII server-command / string-info frames (STATUS#, cxzt#, Battery:..).

  Byte layout mirrors the project's own parser, server/protocol_GT06.py, which is
  authoritative for offsets and quirks. Bit meanings are per the Concox GT06 v1.8.1
  spec (gt06/GT06_protocol_v1.8.1.txt).

  Install: copy to your Wireshark "Personal Lua Plugins" folder
  (Help > About Wireshark > Folders), then Analyze > Reload Lua Plugins. See README.md.

  Plaintext protocol — no keys involved.
]]--

local GT06_PORT = 7711   -- adjust via "Decode As" if your capture uses another port

local gt06 = Proto("gt06", "GT06 GPS Tracker")

----------------------------------------------------------------------
-- Value strings
----------------------------------------------------------------------
local PROTO_NAMES = {
  [0x01] = "Login",
  [0x12] = "Location (v1.8)",
  [0x22] = "Location (v3)",
  [0x13] = "Heartbeat / Status",
  [0x16] = "Alarm / SOS",
  [0x23] = "Alarm / SOS (v3)",
  [0x15] = "String Info (response)",
  [0x80] = "Server Command",
  [0x1A] = "Address/Phone Query",
  [0x8A] = "Time Sync",
}

local VOLTAGE_NAMES = {
  [0] = "0 No power (shutdown)", [1] = "1 Extremely low", [2] = "2 Very low",
  [3] = "3 Low", [4] = "4 Medium", [5] = "5 High", [6] = "6 Very high",
}
local GSM_NAMES = {
  [0] = "0 No signal", [1] = "1 Extremely weak", [2] = "2 Very weak",
  [3] = "3 Good", [4] = "4 Strong",
}
local ALARM_BITS = {
  [0] = "Normal", [1] = "Shock", [2] = "Power Cut", [3] = "Low Battery", [4] = "SOS",
}
local ALARM_STATUS = {
  [0] = "Normal", [1] = "SOS", [2] = "Power Cut", [3] = "Shock",
  [4] = "Fence In", [5] = "Fence Out",
}
local LANG_NAMES = { [1] = "Chinese", [2] = "English" }
local NS_NAMES = { [0] = "South", [1] = "North" }   -- code: is_south = not bit10
local EW_NAMES = { [0] = "East", [1] = "West" }
local FIX_NAMES = { [0] = "Not positioned", [1] = "GPS positioned" }
local OILELEC   = { [0] = "Connected", [1] = "Disconnected" }
local ONOFF     = { [0] = "Off", [1] = "On" }
local CHG_NAMES = { [0] = "Not charging", [1] = "Charging" }
local ACC_NAMES = { [0] = "Low", [1] = "High" }
local ACT_NAMES = { [0] = "Deactivated", [1] = "Activated" }

----------------------------------------------------------------------
-- Fields
----------------------------------------------------------------------
local f = gt06.fields
-- framing
f.start    = ProtoField.uint16("gt06.start",    "Start bytes",  base.HEX)
f.length   = ProtoField.uint32("gt06.length",   "Length",       base.DEC)
f.protocol = ProtoField.uint8 ("gt06.protocol", "Protocol",     base.HEX, PROTO_NAMES)
f.dir      = ProtoField.string("gt06.direction","Direction")
f.payload  = ProtoField.bytes ("gt06.payload",  "Payload")
f.serial   = ProtoField.uint16("gt06.serial",   "Serial #",     base.DEC)
f.crc      = ProtoField.uint16("gt06.crc",      "CRC-ITU",      base.HEX)
f.crc_calc = ProtoField.uint16("gt06.crc_calc", "CRC-ITU (calculated)", base.HEX)
f.crc_ok   = ProtoField.bool  ("gt06.crc_ok",   "CRC valid")
f.stop     = ProtoField.uint16("gt06.stop",     "Stop bytes",   base.HEX)
-- login
f.imei     = ProtoField.string("gt06.imei",     "IMEI")
-- location
f.loc_time = ProtoField.string("gt06.loc.time", "Timestamp (UTC)")
f.gpsinfo  = ProtoField.uint8 ("gt06.loc.gpsinfo",  "GPS info byte", base.HEX)
f.infolen  = ProtoField.uint8 ("gt06.loc.infolen",  "GPS info length nibble", base.DEC, nil, 0xF0)
f.sats     = ProtoField.uint8 ("gt06.loc.sats",     "Satellites",  base.DEC, nil, 0x0F)
f.lat_raw  = ProtoField.uint32("gt06.loc.lat_raw",  "Latitude (raw)",  base.DEC)
f.lon_raw  = ProtoField.uint32("gt06.loc.lon_raw",  "Longitude (raw)", base.DEC)
f.lat      = ProtoField.double("gt06.loc.lat",      "Latitude (deg)")
f.lon      = ProtoField.double("gt06.loc.lon",      "Longitude (deg)")
f.speed    = ProtoField.uint8 ("gt06.loc.speed",    "Speed (km/h)", base.DEC)
f.course   = ProtoField.uint16("gt06.loc.course_status", "Course/Status", base.HEX)
f.heading  = ProtoField.uint16("gt06.loc.heading",  "Heading (deg)", base.DEC, nil, 0x03FF)
f.ns       = ProtoField.uint16("gt06.loc.ns",       "Lat hemisphere", base.DEC, NS_NAMES,  0x0400)
f.ew       = ProtoField.uint16("gt06.loc.ew",       "Lon hemisphere", base.DEC, EW_NAMES,  0x0800)
f.fix      = ProtoField.uint16("gt06.loc.fix",      "GPS fix",        base.DEC, FIX_NAMES, 0x1000)
-- LBS (cell tower)
f.mcc      = ProtoField.uint16("gt06.lbs.mcc",   "MCC", base.DEC)
f.mnc      = ProtoField.uint8 ("gt06.lbs.mnc",   "MNC", base.DEC)
f.lac      = ProtoField.uint16("gt06.lbs.lac",   "LAC", base.DEC)
f.cellid   = ProtoField.uint32("gt06.lbs.cellid","Cell ID", base.DEC)
-- terminal info (heartbeat / alarm)
f.terminfo = ProtoField.uint8("gt06.term.info", "Terminal info", base.HEX)
f.ti_oil   = ProtoField.uint8("gt06.term.oilelec","Oil/electricity", base.DEC, OILELEC,  0x80)
f.ti_gps   = ProtoField.uint8("gt06.term.gps",    "GPS tracking",    base.DEC, ONOFF,    0x40)
f.ti_alarm = ProtoField.uint8("gt06.term.alarm",  "Alarm",           base.DEC, ALARM_BITS, 0x38)
f.ti_chg   = ProtoField.uint8("gt06.term.charge", "Charge",          base.DEC, CHG_NAMES,0x04)
f.ti_acc   = ProtoField.uint8("gt06.term.acc",    "ACC",             base.DEC, ACC_NAMES,0x02)
f.ti_act   = ProtoField.uint8("gt06.term.activated","Defense",       base.DEC, ACT_NAMES,0x01)
f.voltage  = ProtoField.uint8("gt06.voltage_level","Voltage level",  base.DEC, VOLTAGE_NAMES)
f.gsm      = ProtoField.uint8("gt06.gsm",          "GSM signal",     base.DEC, GSM_NAMES)
f.alarmlang= ProtoField.uint16("gt06.alarm_lang",  "Alarm/Language", base.HEX)
f.al_status= ProtoField.uint16("gt06.alarm_status","Alarm status",   base.DEC, ALARM_STATUS, 0xFF00)
f.al_lang  = ProtoField.uint16("gt06.language",    "Language",       base.DEC, LANG_NAMES,   0x00FF)
-- string command / response
f.content_len = ProtoField.uint8 ("gt06.str.content_len", "Content length", base.DEC)
f.server_flag = ProtoField.bytes ("gt06.str.server_flag", "Server flag")
f.command     = ProtoField.string("gt06.str.command",     "Command")
f.response    = ProtoField.string("gt06.str.response",    "Response")

-- expert info
local ef_crc = ProtoExpert.new("gt06.crc.bad", "Bad CRC-ITU", expert.group.CHECKSUM, expert.severity.WARN)
gt06.experts = { ef_crc }

----------------------------------------------------------------------
-- CRC-ITU (CRC-16/X.25): poly 0x8408 reflected, init 0xFFFF, xorout 0xFFFF.
-- Pure-Lua (no bit library dependency) so it runs on any Wireshark Lua build.
----------------------------------------------------------------------
local function bxor16(a, b)
  local r, p = 0, 1
  for _ = 0, 15 do
    local abit = a % 2; local bbit = b % 2
    if abit ~= bbit then r = r + p end
    a = (a - abit) / 2; b = (b - bbit) / 2; p = p * 2
  end
  return r
end

-- range: a TvbRange covering the bytes to checksum
local function crc_itu(range)
  local bytes = range:bytes()
  local crc = 0xFFFF
  for i = 0, bytes:len() - 1 do
    crc = bxor16(crc, bytes:get_index(i))
    for _ = 1, 8 do
      if crc % 2 == 1 then
        crc = bxor16(math.floor(crc / 2), 0x8408)
      else
        crc = math.floor(crc / 2)
      end
    end
  end
  return bxor16(crc, 0xFFFF)
end

----------------------------------------------------------------------
-- Helpers
----------------------------------------------------------------------
local function imei_from_bytes(range)
  -- BCD nibbles; mirrors gt06_parse_login(): hex, strip leading zeros, drop a
  -- leading pad nibble if 16 hex chars remain (15-digit IMEI).
  local hex = range:bytes():tohex(true)        -- lowercase, no spaces
  hex = hex:gsub("^0+", "")
  if #hex == 16 then hex = hex:sub(2) end
  if hex == "" then hex = "0" end
  return hex
end

-- Decode the 18-byte GPS block into a subtree; returns lat, lon, sats, fix, summary.
local function dissect_gps(tree, range)
  local g = tree:add(gt06, range, "GPS data")
  -- datetime: raw binary bytes (NOT BCD), per server parser
  local yy, mo, dd = range(0,1):uint(), range(1,1):uint(), range(2,1):uint()
  local hh, mi, ss = range(3,1):uint(), range(4,1):uint(), range(5,1):uint()
  local tstr = string.format("20%02d-%02d-%02d %02d:%02d:%02d", yy, mo, dd, hh, mi, ss)
  g:add(f.loc_time, range(0,6), tstr)

  local gpsinfo = range(6,1)
  local gi = g:add(f.gpsinfo, gpsinfo)
  gi:add(f.infolen, gpsinfo)
  gi:add(f.sats, gpsinfo)
  local sats = gpsinfo:uint() % 16

  local lat_raw = range(7,4):uint()
  local lon_raw = range(11,4):uint()
  local course  = range(16,2):uint()
  local is_south = (math.floor(course / 0x400) % 2) == 0   -- is_south = not bit10
  local is_west  = (math.floor(course / 0x800) % 2) == 1
  local fix      = (math.floor(course / 0x1000) % 2) == 1
  local lat = lat_raw / 1800000.0; if is_south then lat = -lat end
  local lon = lon_raw / 1800000.0; if is_west  then lon = -lon end

  g:add(f.lat_raw, range(7,4))
  g:add(f.lat,     range(7,4), lat)
  g:add(f.lon_raw, range(11,4))
  g:add(f.lon,     range(11,4), lon)

  local spd = range(15,1)
  g:add(f.speed, spd):append_text(string.format("  (%.1f kn)", spd:uint() / 1.852))

  local c = g:add(f.course, range(16,2))
  c:add(f.heading, range(16,2))
  c:add(f.ns, range(16,2))
  c:add(f.ew, range(16,2))
  c:add(f.fix, range(16,2))

  local summary = string.format("%.5f,%.5f sat=%d %s", lat, lon, sats,
                                 fix and "GPS" or "no-fix")
  return lat, lon, sats, fix, summary
end

-- terminal-info + voltage + gsm + alarm/lang block (heartbeat & alarm tail).
local function dissect_status(tree, range)
  local ti = range(0,1)
  local t = tree:add(f.terminfo, ti)
  t:add(f.ti_oil, ti); t:add(f.ti_gps, ti); t:add(f.ti_alarm, ti)
  t:add(f.ti_chg, ti); t:add(f.ti_acc, ti); t:add(f.ti_act, ti)
  local alarm = math.floor(ti:uint() / 8) % 8
  local charging = (math.floor(ti:uint() / 4) % 2) == 1
  local volt, gsm
  if range:len() >= 2 then volt = range(1,1):uint(); tree:add(f.voltage, range(1,1)) end
  if range:len() >= 3 then gsm  = range(2,1):uint(); tree:add(f.gsm, range(2,1)) end
  if range:len() >= 5 then
    local al = tree:add(f.alarmlang, range(3,2))
    al:add(f.al_status, range(3,2)); al:add(f.al_lang, range(3,2))
  end
  return alarm, charging, volt, gsm
end

----------------------------------------------------------------------
-- Dissect one complete frame (ftvb spans exactly one frame).
-- len_size is 1 (0x7878) or 2 (0x7979). Returns an Info-column string.
----------------------------------------------------------------------
local function dissect_frame(ftvb, pinfo, tree, len_size)
  local flen = ftvb:len()
  local proto_off = 2 + len_size
  local proto = ftvb(proto_off, 1):uint()
  local payload_off = proto_off + 1
  local payload_len = flen - payload_off - 6      -- minus serial(2)+crc(2)+stop(2)
  if payload_len < 0 then payload_len = 0 end
  local crc_off = flen - 4

  -- direction from the registered port
  local dir = "?"
  if pinfo.src_port == GT06_PORT then dir = "S->D"
  elseif pinfo.dst_port == GT06_PORT then dir = "D->S" end

  local pname = PROTO_NAMES[proto] or string.format("Unknown 0x%02X", proto)
  local is_ack = (payload_len == 0)               -- short reply: 78 78 05 PROTO serial crc
  if is_ack then pname = "ACK (" .. (PROTO_NAMES[proto] or string.format("0x%02X", proto)) .. ")" end

  local st = tree:add(gt06, ftvb(), string.format("GT06 %s, len %d", pname, flen))
  st:add(f.start, ftvb(0,2))
  st:add(f.length, ftvb(2, len_size))
  st:add(f.protocol, ftvb(proto_off,1))
  st:add(f.dir, ftvb(0,0), dir):set_generated()

  local info
  if is_ack then
    info = string.format("ACK 0x%02X", proto)
  elseif proto == 0x01 then
    local imei = imei_from_bytes(ftvb(payload_off, payload_len))
    st:add(f.imei, ftvb(payload_off, payload_len), imei)
    info = "Login IMEI=" .. imei
  elseif proto == 0x12 or proto == 0x22 then
    local _, _, _, _, summary = dissect_gps(st, ftvb(payload_off, 18))
    -- optional LBS after the 18-byte block
    if payload_len >= 18 + 8 then
      local lo = payload_off + 18
      local lbs = st:add(gt06, ftvb(lo, 8), "LBS (cell tower)")
      lbs:add(f.mcc,    ftvb(lo, 2))
      lbs:add(f.mnc,    ftvb(lo+2, 1))
      lbs:add(f.lac,    ftvb(lo+3, 2))
      lbs:add(f.cellid, ftvb(lo+5, 3), ftvb(lo+5,3):uint())
    end
    info = "Loc " .. summary
  elseif proto == 0x13 then
    local alarm, charging, volt, gsm = dissect_status(st, ftvb(payload_off, payload_len))
    info = string.format("HB volt=%s sig=%s%s", tostring(volt or "?"),
                         tostring(gsm or "?"), charging and " chg" or "")
  elseif proto == 0x16 or proto == 0x23 then
    local _, _, _, fix, summary = dissect_gps(st, ftvb(payload_off, 18))
    local lbs_len = ftvb(payload_off + 18, 1):uint()
    st:add(gt06, ftvb(payload_off+18, 1), "LBS length: " .. lbs_len)
    local soff = payload_off + 19 + lbs_len
    local alarm = 0
    if soff < crc_off then
      alarm = dissect_status(st, ftvb(soff, crc_off - soff))
    end
    info = string.format("ALARM[%s] %s", ALARM_BITS[alarm] or "?", summary)
  elseif proto == 0x80 then
    local clen = ftvb(payload_off, 1):uint()
    st:add(f.content_len, ftvb(payload_off, 1))
    st:add(f.server_flag, ftvb(payload_off+1, 4))
    local cmd_len = payload_len - 5
    local cmd = ""
    if cmd_len > 0 then
      cmd = ftvb(payload_off+5, cmd_len):string()
      st:add(f.command, ftvb(payload_off+5, cmd_len))
    end
    info = "CMD " .. cmd
  elseif proto == 0x15 then
    st:add(f.server_flag, ftvb(payload_off, 4))
    st:add(f.content_len, ftvb(payload_off+4, 1))
    local txt_len = payload_len - 5
    local txt = ""
    if txt_len > 0 then
      txt = ftvb(payload_off+5, txt_len):string()
      st:add(f.response, ftvb(payload_off+5, txt_len))
    end
    info = "RESP " .. txt
  else
    if payload_len > 0 then st:add(f.payload, ftvb(payload_off, payload_len)) end
    info = pname
  end

  -- serial, crc (with verification), stop
  st:add(f.serial, ftvb(flen-6, 2))
  local crc_rx = ftvb(crc_off, 2):uint()
  local crc_cl = crc_itu(ftvb(2, crc_off - 2))    -- length byte .. serial inclusive
  local crc_node = st:add(f.crc, ftvb(crc_off, 2))
  crc_node:add(f.crc_calc, ftvb(crc_off,2), crc_cl):set_generated()
  local ok_node = st:add(f.crc_ok, ftvb(crc_off,2), crc_rx == crc_cl):set_generated()
  if crc_rx ~= crc_cl then
    crc_node:add_proto_expert_info(ef_crc, string.format("got 0x%04X, computed 0x%04X", crc_rx, crc_cl))
    info = info .. " [BAD CRC]"
  end
  st:add(f.stop, ftvb(flen-2, 2))

  return info
end

----------------------------------------------------------------------
-- Top-level dissector: walk the TCP stream, desegmenting as needed.
----------------------------------------------------------------------
function gt06.dissector(tvb, pinfo, tree)
  local len = tvb:len()
  if len == 0 then return 0 end
  local offset = 0
  local infos = {}

  while offset < len do
    if len - offset < 5 then                      -- need start(2)+len(1)+proto(1)+...
      pinfo.desegment_offset = offset
      pinfo.desegment_len = DESEGMENT_ONE_MORE_SEGMENT
      break
    end
    local start = tvb(offset, 2):uint()
    local len_size, frame_len
    if start == 0x7878 then
      len_size = 1
      frame_len = 2 + 1 + tvb(offset+2, 1):uint() + 2
    elseif start == 0x7979 then
      len_size = 2
      frame_len = 2 + 2 + tvb(offset+2, 2):uint() + 2
    else
      -- not our framing; if nothing decoded yet, decline so others can try
      if offset == 0 then return 0 end
      break
    end
    if len - offset < frame_len then
      pinfo.desegment_offset = offset
      pinfo.desegment_len = frame_len - (len - offset)
      break
    end
    local info = dissect_frame(tvb(offset, frame_len):tvb(), pinfo, tree, len_size)
    if info then infos[#infos + 1] = info end
    offset = offset + frame_len
  end

  pinfo.cols.protocol = "GT06"
  if #infos > 0 then
    pinfo.cols.info = table.concat(infos, " | ")
  end
  return offset
end

DissectorTable.get("tcp.port"):add(GT06_PORT, gt06)

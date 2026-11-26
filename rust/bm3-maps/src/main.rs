//! bm3-maps: bootmod3 CustomROM controls for the Bootmod3 Suite app (src/bm3_maps.cpp). A port of src/collector.c's
//! BM3_MAPS_ONLY worker that sends the same requests in the same order, prints the same line and exits the same way.
//!   bm3-maps --host IPv4 --status       identifies the ROM and reads the active slot; writes no memory
//!   bm3-maps --host IPv4 --map 1..4     identifies the ROM, writes the slot and reads it back
//!   bm3-maps --host IPv4 --sound 0|1    Active Sound off or on
//!   bm3-maps --host IPv4 --burbles 0..3 burbles off, or aggression 4, 8 or 12; written and read back
//! Every run identifies the ROM again, and nothing is written unless the marker names a known ROM. --marker HEX sets
//! the marker's address; otherwise /var/local/data/EntryNavApps/bm3-marker-address, otherwise each address bootmod3's
//! own app tries (MARKER_ADDRESSES). --dump shows each step and the marker's bytes on stderr; it only adds output.
//! --search-dev, --search-port and --gateway-file are for the tests, as in the other workers. Exit 0 when the line
//! reports the active map or the DME accepted the change, 1 otherwise, 2 for wrong arguments.
//!
//! What writes to the DME, all with the profile's write service (3D, or 3E answered 7E 3D) after the ROM is known:
//!   --map N      3x 14 <map address> 02 <N - 1> 01, then 23 14 <map address> 01 reads it back
//!   --burbles P  on: 3x 14 <timing address> 18 <24 bytes>, then 3x 14 <burble address> 01 01; off: the switch (00)
//!                first, then the table (zeros). The second write only after the first was answered positively. Both
//!                are read back with 23 14, the table first.
//!   --sound S    2E 5F FD 00 (on) or 01 (off), 8 s to answer
//! Identifying the ROM writes no memory: 2C 03 F3 01 and 2C 02 F3 01 24 <address> 00 FF (re)define the dynamic DID
//! F301 in the DME's RAM, and 22 F3 01 or 23 14 <address> FF read the marker.

use bm3_maps::{
    burble_table, clear_dynamic_did, define_marker_did, marker_profile, read_marker_did, read_memory, reply_prefix,
    slot_reply, sound_request, write_memory, Profile, BURBLE_PRESETS, BURBLE_STEP, BURBLE_TABLE, DME, MARKER_SIZE,
    SLOTS,
};
use ena_diag::gateway::{GatewaySearch, SEARCH_FAILURES};
use ena_diag::hsfz::{connect_gw, exchange, Reply};
use ena_diag::lease::{release_gateway, reserve_gateway};
use ena_diag::{arguments, number, program_name, strtoul_base, warn, Output};
use std::os::fd::OwnedFd;

/// Where the marker address set by hand lives (ENA_DATA_DIR "/bm3-marker-address" of src/paths.h).
const MARKER_FILE: &str = "/var/local/data/EntryNavApps/bm3-marker-address";

/// Where bootmod3's own app looks for the marker, in its order: MG1 first, then the F-series MEVD17 (0977FE4C, seen on
/// an F20 M140i with R1C900000) and two more DME families. Captured by Noahbg/mgu-apps (hu-obd-adapter).
const MARKER_ADDRESSES: [u32; 4] = [0x807F_FE50, 0x0977_FE4C, 0x801F_FDAC, 0x8005_FE1E];

/// bm3_protocol.js's timeouts: 4.5 s for most requests and 8 s for Active Sound.
const TIMEOUT_MS: u64 = 4500;
const SOUND_TIMEOUT_MS: u64 = 8000;

/// What the C reads each answer into: a reply longer than its buffer fails the request.
const MARKER_REPLY: usize = 1024; // room for a wrong-length marker, reported as such
const SHORT_REPLY: usize = 8;
const HOLDS_REPLY: usize = 8 + BURBLE_TABLE;
const SOUND_REPLY: usize = 16;
/// The write request's buffer (8 + BM3_BURBLE_TABLE).
const WRITE_REQUEST: usize = 8 + BURBLE_TABLE;

struct Dme<'a> {
    fd: &'a OwnedFd,
    /// --dump: each identification step's result and the marker's bytes, on stderr.
    dumping: bool,
}

impl Dme<'_> {
    /// One request to the DME with the reply prefix its service gets (bm3_exchange_within). An empty request, a builder
    /// that refused its arguments, sends nothing and fails.
    fn exchange_within(&self, uds: &[u8], reply_limit: usize, timeout_ms: u64) -> Reply {
        if uds.is_empty() {
            return Reply::Failed;
        }
        exchange(self.fd, DME, uds, &reply_prefix(uds), reply_limit, timeout_ms)
    }

    fn exchange(&self, uds: &[u8], reply_limit: usize) -> Reply {
        self.exchange_within(uds, reply_limit, TIMEOUT_MS)
    }

    fn note(&self, step: &str, reply: &Reply) {
        if !self.dumping {
            return;
        }
        warn(
            match reply {
                Reply::Refused(nrc) => format!("{step}: refused, NRC {nrc:02X}\n"),
                Reply::Failed => format!("{step}: no answer\n"),
                Reply::Data(data) => format!("{step}: {} bytes\n", data.len()),
            }
            .as_bytes(),
        );
    }

    /// The marker's bytes, 16 to a line in hex and as text (bm3_dump).
    fn dump(&self, data: &[u8]) {
        if !self.dumping {
            return;
        }
        for (row, chunk) in data.chunks(16).enumerate() {
            let mut line = format!("{:04x} ", row * 16);
            for j in 0..16 {
                match chunk.get(j) {
                    Some(byte) => line.push_str(&format!(" {byte:02x}")),
                    None => line.push_str("   "),
                }
            }
            line.push_str("  ");
            line.extend(
                chunk
                    .iter()
                    .map(|&c| if (0x20..0x7f).contains(&c) { c as char } else { '.' }),
            );
            line.push('\n');
            warn(line.as_bytes());
        }
    }

    /// Reads the CustomROM marker at one address: through a dynamic DID, or straight from memory when the DME refuses
    /// that (bm3_identify). On failure: kind 1 for a refusal (try the next address), 2 for a marker that names no known
    /// ROM, 3 when the DME stopped answering, with the line to show.
    fn identify(&self, address: u32) -> Result<&'static Profile, (u32, String)> {
        let lost = || (3, "The DME did not answer".to_string());
        if self.dumping {
            warn(format!("marker at {address:08X}\n").as_bytes());
        }
        let mut marker = None;
        let reply = self.exchange(&clear_dynamic_did(), MARKER_REPLY);
        self.note("2C 03 F3 01", &reply);
        let mut refused = matches!(reply, Reply::Refused(_));
        if reply == Reply::Failed {
            return Err(lost());
        }
        if !refused {
            let reply = self.exchange(&define_marker_did(address), MARKER_REPLY);
            self.note("2C 02 F3 01", &reply);
            refused = matches!(reply, Reply::Refused(_));
            if reply == Reply::Failed {
                return Err(lost());
            }
        }
        if !refused {
            let reply = self.exchange(&read_marker_did(), MARKER_REPLY);
            self.note("22 F3 01", &reply);
            match reply {
                Reply::Refused(_) => refused = true,
                Reply::Failed => return Err(lost()),
                Reply::Data(data) => marker = Some(data[3..].to_vec()), // after 62 F3 01
            }
        }
        if refused {
            let reply = self.exchange(&read_memory(address, MARKER_SIZE as u32), MARKER_REPLY);
            self.note("23 14", &reply);
            match reply {
                Reply::Refused(nrc) => return Err((1, format!("The DME refused to read its memory (NRC {nrc:02X})"))),
                Reply::Failed => return Err(lost()),
                Reply::Data(data) => marker = Some(data[1..].to_vec()), // after 63
            }
        }
        let marker = marker.unwrap_or_default();
        self.dump(&marker);
        marker_profile(&marker).map_err(|why| (2, format!("No known bootmod3 ROM: {why}")))
    }

    /// The profile at the first address whose marker names a known ROM, or the line to show (bm3_find). The line keeps
    /// the most telling failure: a marker read beats a refusal, and a lost DME ends the search.
    fn find(&self, addresses: &[u32]) -> Result<&'static Profile, String> {
        let mut message = "No bootmod3 CustomROM marker found".to_string();
        let mut best = 0;
        for &address in addresses {
            match self.identify(address) {
                Ok(profile) => return Ok(profile),
                Err((kind, attempt)) => {
                    if kind >= best {
                        best = kind;
                        message = attempt;
                    }
                    if kind == 3 {
                        break;
                    }
                }
            }
        }
        Err(message)
    }

    /// The active slot, 0..3 (bm3_read_slot).
    fn read_slot(&self, profile: &Profile) -> Option<u32> {
        match self.exchange(&read_memory(profile.map_address, 1), SHORT_REPLY) {
            Reply::Data(data) => slot_reply(&data),
            _ => None,
        }
    }

    /// size bytes at address with the profile's memory-write service (bm3_write).
    fn write(&self, profile: &Profile, address: u32, data: &[u8]) -> Reply {
        self.exchange(&write_memory(WRITE_REQUEST, profile, address, data), SHORT_REPLY)
    }

    /// Whether the DME's memory at address reads back as data (bm3_holds).
    fn holds(&self, address: u32, data: &[u8]) -> bool {
        matches!(self.exchange(&read_memory(address, data.len() as u32), HOLDS_REPLY),
                 Reply::Data(reply) if reply.len() == data.len() + 1 && reply[1..] == *data)
    }

    /// Map slot 1..4: written with its activation flag, then read back (bm3_set_map). The exit status.
    fn set_map(&self, out: &mut Output, profile: &Profile, slot: u32) -> i32 {
        let data = [(slot - 1) as u8, 0x01]; // the slot, then activate
        if let Reply::Refused(nrc) = self.write(profile, profile.map_address, &data) {
            out.line(format!("The DME refused the map change (NRC {nrc:02X})"));
            return 1;
        }
        match self.read_slot(profile) {
            None => {
                out.line("Map change not confirmed. Check the active map before trying again.");
                1
            }
            Some(active) if active != slot - 1 => {
                out.line(format!("Map change not confirmed: the DME reports map {}", active + 1));
                1
            }
            Some(active) => {
                out.line(format!("Map {} active", active + 1));
                0
            }
        }
    }

    /// Burble preset 0..3 (burble_table). Off stops the DME using the table before clearing it; on fills the table
    /// before turning it on. Both are read back (bm3_set_burbles).
    fn set_burbles(&self, out: &mut Output, profile: &Profile, preset: u32) -> i32 {
        let Some(table) = burble_table(profile, preset) else {
            out.line("No burble table for this ROM");
            return 1;
        };
        let enabled = [u8::from(preset != 0)];
        let (first, second) = if preset != 0 {
            (
                (profile.timing_address, &table[..]),
                (profile.burble_address, &enabled[..]),
            )
        } else {
            (
                (profile.burble_address, &enabled[..]),
                (profile.timing_address, &table[..]),
            )
        };
        let mut reply = self.write(profile, first.0, first.1);
        if matches!(reply, Reply::Data(ref d) if !d.is_empty()) {
            reply = self.write(profile, second.0, second.1);
        }
        if let Reply::Refused(nrc) = reply {
            out.line(format!("The DME refused the burble change (NRC {nrc:02X})"));
            return 1;
        }
        if !self.holds(profile.timing_address, &table) || !self.holds(profile.burble_address, &enabled) {
            out.line("Burble change not confirmed. Try again before driving.");
            return 1;
        }
        if preset != 0 {
            out.line(format!(
                "Burbles at aggression {}, read back from the DME",
                preset * BURBLE_STEP
            ));
        } else {
            out.line("Burbles off, read back from the DME");
        }
        0
    }

    /// Active Sound off or on: one request the DME acknowledges. It can't be read back (bm3_set_sound).
    fn set_sound(&self, out: &mut Output, on: bool) -> i32 {
        let what = if on { "Active Sound on" } else { "Active Sound off" };
        match self.exchange_within(&sound_request(on), SOUND_REPLY, SOUND_TIMEOUT_MS) {
            Reply::Data(ref d) if !d.is_empty() => {
                out.line(format!("The DME accepted: {what}"));
                return 0;
            }
            Reply::Refused(nrc) => out.line(format!("The DME refused {what} (NRC {nrc:02X})")),
            _ => out.line(format!("No answer from the DME for {what}")),
        }
        1
    }
}

/// The text of the marker address set by hand: --marker, cut to 31 bytes as the C's text[32] holds it, or the first
/// line of MARKER_FILE as fgets reads it, each up to a NUL, CR or LF.
fn marker_text(option: Option<&[u8]>) -> Vec<u8> {
    let mut text = match option {
        Some(option) => option[..option.len().min(31)].to_vec(),
        None => {
            let data = std::fs::read(MARKER_FILE).unwrap_or_default();
            let line = data
                .iter()
                .position(|&c| c == b'\n')
                .map_or(data.len(), |newline| newline + 1);
            data[..line.min(31)].to_vec()
        }
    };
    if let Some(end) = text.iter().position(|&c| c == 0) {
        text.truncate(end); // a C string ends at its NUL
    }
    if let Some(end) = text.iter().position(|&c| c == b'\r' || c == b'\n') {
        text.truncate(end);
    }
    text
}

/// The marker address set by hand, or None (bm3_marker_address). Eight hex digits as strtoul reads them, or "Invalid
/// marker address" and exit 2.
#[allow(clippy::unnecessary_cast)] // unsigned long is 32 bits on the head unit, 64 on a PC
fn marker_address(option: Option<&[u8]>) -> Option<u32> {
    let text = marker_text(option);
    if text.is_empty() {
        return None;
    }
    let (value, rest) = strtoul_base(&text, 16);
    if text.len() != 8 || rest {
        warn(&[b"Invalid marker address: ", &text[..], b"\n"].concat());
        std::process::exit(2);
    }
    Some(value as u32)
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Action {
    None,
    Status,
    Map,
    Sound,
    Burbles,
}

fn main() {
    let args = arguments();
    let mut host_option: Option<&[u8]> = None;
    let mut marker_option: Option<&[u8]> = None;
    let mut port = 6801u16;
    let mut action = Action::None;
    let mut value = 0;
    let mut dumping = false;
    let mut search = GatewaySearch::default();
    let mut i = 1;
    while i < args.len() {
        let has_next = i + 1 < args.len();
        let has_value = has_next && action == Action::None;
        match args[i].as_slice() {
            b"--host" if has_next && host_option.is_none() => {
                i += 1;
                host_option = Some(&args[i]);
            }
            b"--port" if has_next => {
                i += 1;
                port = number(&args[i], 1, 65535) as u16;
            }
            b"--marker" if has_next => {
                i += 1;
                marker_option = Some(&args[i]);
            }
            b"--status" if action == Action::None => action = Action::Status,
            b"--map" if has_value => {
                i += 1;
                action = Action::Map;
                value = number(&args[i], 1, SLOTS);
            }
            b"--sound" if has_value => {
                i += 1;
                action = Action::Sound;
                value = number(&args[i], 0, 1);
            }
            b"--burbles" if has_value => {
                i += 1;
                action = Action::Burbles;
                value = number(&args[i], 0, BURBLE_PRESETS);
            }
            b"--dump" => dumping = true,
            _ if search.option(&args, &mut i) => {}
            _ => std::process::exit(2),
        }
        i += 1;
    }
    // The C copies the host into 16 bytes (INET_ADDRSTRLEN) only when it fits.
    let mut host = host_option
        .filter(|h| h.len() < 16)
        .map(<[u8]>::to_vec)
        .unwrap_or_default();
    if host.is_empty() || action == Action::None {
        warn(
            &[
                b"Usage: ",
                program_name(&args),
                b" --host IPv4 [--port 6801] [--marker HEX] [--dump] ",
                b"(--status | --map 1-4 | --sound 0-1 | --burbles 0-3)\n",
            ]
            .concat(),
        );
        std::process::exit(2);
    }
    let marker = marker_address(marker_option);
    ena_diag::handle_signals();
    let mut out = Output::default();
    let status = run(&mut out, &mut host, port, &mut search, marker, action, value, dumping);
    out.flush();
    std::process::exit(status);
}

#[allow(clippy::too_many_arguments)]
fn run(
    out: &mut Output,
    host: &mut Vec<u8>,
    port: u16,
    search: &mut GatewaySearch,
    marker: Option<u32>,
    action: Action,
    value: u32,
    dumping: bool,
) -> i32 {
    // No wait for the lease: a gauge app that holds it gives it up within a lease, and the app's press is tried again.
    let Ok(lock) = reserve_gateway() else {
        out.line("Diagnostics busy. Wait a moment and try again.");
        return 1;
    };
    let mut gateway = connect_gw(host, port);
    if gateway.is_none() {
        search.failures = SEARCH_FAILURES - 1; // a one-shot run searches at once
        if search.after_failure(host, port) {
            gateway = connect_gw(host, port);
        }
    }
    let Some(fd) = gateway else {
        out.line("Diagnostic gateway unavailable");
        release_gateway(None, Some(lock));
        return 1;
    };
    let dme = Dme { fd: &fd, dumping };
    let found = match marker {
        Some(address) => dme.find(&[address]),
        None => dme.find(&MARKER_ADDRESSES),
    };
    let profile = match found {
        Ok(profile) => profile,
        Err(message) => {
            out.line(message);
            release_gateway(Some(fd), Some(lock));
            return 1;
        }
    };
    warn(format!("ROM {}\n", profile.rom).as_bytes()); // for a run by hand; the app shows only the line on stdout
    let result = match action {
        Action::Map => dme.set_map(out, profile, value),
        Action::Burbles => dme.set_burbles(out, profile, value),
        Action::Sound => dme.set_sound(out, value != 0),
        _ => match dme.read_slot(profile) {
            None => {
                out.line("Could not read the active map");
                1
            }
            Some(active) => {
                out.line(format!("Map {} active", active + 1));
                0
            }
        },
    };
    release_gateway(Some(fd), Some(lock));
    result
}

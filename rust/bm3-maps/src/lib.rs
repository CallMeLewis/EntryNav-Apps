//! bootmod3 CustomROM map slots on the DME, a port of src/bm3.c and src/bm3.h, themselves from cauliflowermossgrape's
//! bm3_protocol.js (MIT, third-party/cauliflowermossgrape-LICENSE), itself from BM3 Live by ezl0l. This project is
//! independent of and unaffiliated with bootmod3.
//!
//! Map switching is not a BMW job: bootmod3's code in the DME handles it. The worker reads the CustomROM marker from
//! DME memory and goes on only when it names a ROM in profiles.rs. The map slot and the burble table are then written
//! to the profile's addresses with bootmod3's memory-write command and read back. Active Sound is one request, as
//! bm3_protocol.js sends it. Everything here is pure: building requests and checking replies. The I/O is in main.rs.

mod profiles;

pub use profiles::PROFILES;

pub const DME: u8 = 0x12;
pub const MARKER_SIZE: usize = 0xFF;
pub const SLOTS: u32 = 4;
/// Bytes in the burble timing table (KF_DZWMIN_SPORT_SOT).
pub const BURBLE_TABLE: usize = 24;
/// Off, then aggression 4, 8 and 12.
pub const BURBLE_PRESETS: u32 = 3;
pub const BURBLE_STEP: u32 = 4;

/// One ROM's addresses (bm3_profile).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Profile {
    pub rom: &'static str,
    /// The active slot (0..3), then an activation flag.
    pub map_address: u32,
    /// Burbles on (1) or off (0).
    pub burble_address: u32,
    /// The burble timing table, BURBLE_TABLE signed bytes.
    pub timing_address: u32,
    /// The table's units per degree of aggression.
    pub factor: u32,
    /// 0x3D WriteMemoryByAddress, or 0x3E as bootmod3 uses it on some ROMs.
    pub write_service: u32,
}

/// The profile for a ROM (bm3_profile_for).
pub fn profile_for(rom: &[u8]) -> Option<&'static Profile> {
    PROFILES.iter().find(|p| p.rom.as_bytes() == rom)
}

fn upper_alnum(c: u8) -> bool {
    c.is_ascii_uppercase() || c.is_ascii_digit()
}

fn contains(data: &[u8], needle: &[u8]) -> bool {
    data.windows(needle.len()).any(|w| w == needle)
}

/// binaryMarkerTag: BM3_, 64 binary bytes, ~|~, then underscores and #tags up to a NUL. The ROM tag is the last one,
/// [A-Z0-9] only. MG1 starts at BM3_ with a single tag (upstream's layout):
///     BM3_ <64> ~|~ ___ #R4C9H5V02OKLX0 \0
/// The F-series MEVD17 puts #DST in front, and the stock ROM's tag before the bootmod3 one (an F20 M140i, 2026-11-15):
///     #DST BM3_ <64> ~|~ ____ #EPST:R1C9J8B3B ______ #R1C900000CEKXA \0
/// m holds MARKER_SIZE bytes.
fn binary_marker_tag(m: &[u8]) -> Option<&[u8]> {
    let base = if m.starts_with(b"BM3_") {
        0
    } else if m.starts_with(b"#DSTBM3_") {
        4
    } else {
        return None;
    };
    if &m[base + 68..base + 71] != b"~|~" {
        return None;
    }
    let mut i = base + 71;
    if m[i] != b'_' {
        return None;
    }
    let start = loop {
        while i < MARKER_SIZE && m[i] == b'_' {
            i += 1;
        }
        if i >= MARKER_SIZE || m[i] != b'#' {
            return None;
        }
        i += 1;
        let start = i;
        while i < MARKER_SIZE && (0x20..0x7f).contains(&m[i]) && m[i] != b'_' && m[i] != b'#' {
            i += 1;
        }
        if i >= MARKER_SIZE || i == start {
            return None;
        }
        if m[i] == 0 {
            break start; // the last tag: the ROM's
        }
        if m[i] != b'_' {
            return None;
        }
    };
    let tag = &m[start..i];
    tag.iter().all(|&c| upper_alnum(c)).then_some(tag)
}

/// hashMarkerTag: BM3_, the only # at byte 101, then ROM (9) + build (5) + _ + more of [A-Z0-9_], ended by NUL. The
/// first 9 characters. m holds MARKER_SIZE bytes.
fn hash_marker_rom(m: &[u8]) -> Option<&[u8]> {
    if !m.starts_with(b"BM3_") {
        return None;
    }
    if m.iter().enumerate().any(|(i, &c)| c == b'#' && i != 101) || m[101] != b'#' {
        return None;
    }
    let nul = 102 + m[102..].iter().position(|&c| c == 0)?;
    if nul - 102 < 15 || m[102 + 14] != b'_' {
        return None;
    }
    if !m[102..nul].iter().all(|&c| upper_alnum(c) || c == b'_') || m[102..102 + 14].contains(&b'_') {
        return None;
    }
    Some(&m[102..111])
}

/// The ROM's profile named in the marker's 255 bytes, or why there is none (bm3_marker_profile). Three layouts, as
/// upstream: a binary block (BM3_, 64 bytes, ~|~, underscores, #ROM + 5 build characters, NUL), the S58 one (BM3_,
/// binary, the only # at byte 101, ROM (9) + build (5) + _ + chassis..., NUL), and the older text one (BM3_R somewhere,
/// #ROM). A ROM must match exactly one profile.
pub fn marker_profile(m: &[u8]) -> Result<&'static Profile, &'static str> {
    if m.len() != MARKER_SIZE {
        return Err("marker length");
    }
    if let Some(tag) = binary_marker_tag(m) {
        // The captured MG1 token appends five build characters: R4C9H5V02OKLX0.
        let mut found = PROFILES
            .iter()
            .filter(|p| tag.len() == p.rom.len() + 5 && tag.starts_with(p.rom.as_bytes()));
        return match (found.next(), found.next()) {
            (Some(profile), None) => Ok(profile),
            _ => Err("ROM not uniquely matched in the binary marker"),
        };
    }
    if let Some(rom) = hash_marker_rom(m) {
        return profile_for(rom).ok_or("ROM has no profile");
    }
    if !contains(m, b"BM3_R") {
        return Err("no bootmod3 CustomROM marker");
    }
    let mut found = PROFILES
        .iter()
        .filter(|p| contains(m, &[b"#", p.rom.as_bytes()].concat()));
    match (found.next(), found.next()) {
        (Some(profile), None) => Ok(profile),
        _ => Err("ROM not uniquely matched in the marker"),
    }
}

/// 2C 03 F3 01: clears the dynamic DID F301.
pub fn clear_dynamic_did() -> Vec<u8> {
    vec![0x2C, 0x03, 0xF3, 0x01]
}

/// 2C 02 F3 01 24 AAAAAAAA 00 FF: defines F301 as the marker's 255 bytes at address.
pub fn define_marker_did(address: u32) -> Vec<u8> {
    let mut request = vec![0x2C, 0x02, 0xF3, 0x01, 0x24];
    request.extend_from_slice(&address.to_be_bytes());
    request.extend_from_slice(&[0, MARKER_SIZE as u8]);
    request
}

/// 22 F3 01: reads F301.
pub fn read_marker_did() -> Vec<u8> {
    vec![0x22, 0xF3, 0x01]
}

/// 23 14 AAAAAAAA LL: ReadMemoryByAddress. length is sent as one byte, as the C casts it.
pub fn read_memory(address: u32, length: u32) -> Vec<u8> {
    let mut request = vec![0x23, 0x14];
    request.extend_from_slice(&address.to_be_bytes());
    request.push(length as u8);
    request
}

/// SID 14 AAAAAAAA LL data, the profile's memory write; empty when the service is unknown or the request wouldn't fit
/// in cap bytes (bm3_write_memory's 0).
pub fn write_memory(cap: usize, profile: &Profile, address: u32, data: &[u8]) -> Vec<u8> {
    if profile.write_service != 0x3D && profile.write_service != 0x3E {
        return Vec::new();
    }
    if data.is_empty() || data.len() > 0xFF || 7 + data.len() > cap {
        return Vec::new();
    }
    let mut request = vec![profile.write_service as u8, 0x14];
    request.extend_from_slice(&address.to_be_bytes());
    request.push(data.len() as u8);
    request.extend_from_slice(data);
    request
}

/// Active Sound, DID 5FFD: 2E 5F FD 00 turns it on, 2E 5F FD 01 off.
pub fn sound_request(on: bool) -> Vec<u8> {
    vec![0x2E, 0x5F, 0xFD, if on { 0x00 } else { 0x01 }]
}

/// The burble timing table for preset 0..BURBLE_PRESETS: every byte -(preset * BURBLE_STEP * factor), as a signed
/// byte, so 0 for off. None if the preset or the profile's factor is out of range.
pub fn burble_table(profile: &Profile, preset: u32) -> Option<[u8; BURBLE_TABLE]> {
    if preset > BURBLE_PRESETS || profile.factor == 0 || BURBLE_PRESETS * BURBLE_STEP * profile.factor > 127 {
        return None;
    }
    let degrees = preset * BURBLE_STEP * profile.factor;
    Some([(0x100 - degrees) as u8; BURBLE_TABLE]) // the two's complement byte of -degrees
}

/// The positive reply's first bytes for a request built above: the SID + 0x40, then what the DME echoes.
pub fn reply_prefix(request: &[u8]) -> Vec<u8> {
    let sid = request[0];
    let mut prefix = vec![sid.wrapping_add(0x40)];
    match sid {
        0x2C => prefix.extend_from_slice(&request[1..4]),        // 6C 0X F3 01
        0x22 | 0x2E => prefix.extend_from_slice(&request[1..3]), // 62 F3 01, 6E 5F FD
        0x3E => prefix.push(0x3D),                               // 7E 3D: bootmod3 answers its write this way
        _ => {}                                                  // 63, 7D
    }
    prefix
}

/// A one-byte slot read (63 SS): 0..3, or None.
pub fn slot_reply(reply: &[u8]) -> Option<u32> {
    match reply {
        [0x63, slot] if u32::from(*slot) < SLOTS => Some(u32::from(*slot)),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// profiles.rs and src/bm3_profiles.h come from the same run of tools/make_bm3_profiles.py: the same rows.
    #[test]
    fn the_rust_table_is_the_c_table() {
        let header = include_str!("../../../src/bm3_profiles.h");
        let rows: Vec<String> = header
            .lines()
            .filter(|l| l.starts_with("    {\""))
            .map(str::to_string)
            .collect();
        let ours: Vec<String> = PROFILES
            .iter()
            .map(|p| {
                format!(
                    "    {{\"{}\", 0x{:08X}, 0x{:08X}, 0x{:08X}, {}, 0x{:02X}}},",
                    p.rom, p.map_address, p.burble_address, p.timing_address, p.factor, p.write_service
                )
            })
            .collect();
        assert_eq!(ours, rows);
        let commit = |text: &str| {
            text.lines()
                .nth(1)
                .and_then(|l| l.split_whitespace().nth(2))
                .map(str::to_string)
        };
        assert_eq!(
            commit(include_str!("profiles.rs")),
            commit(header),
            "the same upstream commit"
        );
    }

    #[test]
    fn requests_as_the_c_builds_them() {
        assert_eq!(
            define_marker_did(0x0977_FE4C),
            [0x2C, 2, 0xF3, 1, 0x24, 0x09, 0x77, 0xFE, 0x4C, 0, 0xFF]
        );
        assert_eq!(read_memory(0x4005_E100, 24), [0x23, 0x14, 0x40, 0x05, 0xE1, 0x00, 24]);
        assert_eq!(read_memory(0x1, 0x1FF), [0x23, 0x14, 0, 0, 0, 1, 0xFF]); // the length's low byte, as C's cast
        let profile = profile_for(b"R1C900000").unwrap();
        assert_eq!(write_memory(32, profile, 0x10, &[0xAA; 26]), Vec::<u8>::new()); // 33 bytes don't fit in 32
        assert_eq!(write_memory(32, profile, 0x10, &[0xAA; 25]).len(), 32); // as the C's 7 + length > cap
        assert_eq!(write_memory(32, profile, 0x10, &[0xAA; 24]).len(), 31);
        assert_eq!(write_memory(32, profile, 0x10, &[]), Vec::<u8>::new());
        assert_eq!(reply_prefix(&[0x23, 0x14]), [0x63]);
        assert_eq!(reply_prefix(&[0x3D, 0x14]), [0x7D]);
    }

    #[test]
    fn a_marker_from_the_hash_layout_must_be_upper_case() {
        let mut m = [0u8; MARKER_SIZE];
        m[..4].copy_from_slice(b"BM3_");
        m[101] = b'#';
        m[102..102 + 27].copy_from_slice(b"F4C9L8R02GGGJM_G87_RWD_USA_");
        assert_eq!(marker_profile(&m).map(|p| p.rom), Ok("F4C9L8R02"));
        m[120] = b'a';
        assert_eq!(marker_profile(&m), Err("no bootmod3 CustomROM marker"));
        m[120] = b'A';
        m[110] = b'_'; // an underscore inside the ROM and build
        assert_eq!(marker_profile(&m), Err("no bootmod3 CustomROM marker"));
    }
}

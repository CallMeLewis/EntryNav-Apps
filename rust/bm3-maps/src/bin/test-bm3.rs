//! test-bm3: tests/native/test_bm3.c's checks of src/bm3.c, one for one, run on the Rust port in src/lib.rs. Like the C
//! program it prints each failure on stderr and exits 1, or prints one line and exits 0, so tests/test_bm3_worker.py
//! runs both builds of it the same way. cargo test runs the same checks.

use bm3_maps::{
    burble_table, clear_dynamic_did, define_marker_did, marker_profile, profile_for, read_marker_did, read_memory,
    reply_prefix, slot_reply, sound_request, write_memory, Profile, BURBLE_PRESETS, MARKER_SIZE, PROFILES,
};

const CAPTURED_HEX: &str = "1141F2144D88227FC9D6EBF6C0CEE74104BFF6652CF4A3F310CDC33BB4EF1387D7DA9\
                            11E2F2C0A906D91D631DE69E83FB14CF2A22CEA7161831C7D085133B3CC";

/// BM3_, the captured 64-byte block, ~|~, 30 underscores, #token, NUL, the rest 0xFF.
fn binary_marker(token: &str) -> [u8; MARKER_SIZE] {
    let mut m = [0xFF; MARKER_SIZE];
    m[..4].copy_from_slice(b"BM3_");
    for i in 0..64 {
        m[4 + i] = u8::from_str_radix(&CAPTURED_HEX[2 * i..2 * i + 2], 16).unwrap();
    }
    let mut at = 68;
    m[at..at + 3].copy_from_slice(b"~|~");
    at += 3;
    m[at..at + 30].fill(b'_');
    at += 30;
    m[at] = b'#';
    at += 1;
    m[at..at + token.len()].copy_from_slice(token.as_bytes());
    m[at + token.len()] = 0;
    m
}

/// The F-series MEVD17 layout, as read at 0977FE4C on an F20 M140i: #DST, then the binary layout above with the stock
/// ROM's tag before the bootmod3 one. The 64 bytes are the MG1 capture's, not the car's.
fn mevd17_marker(stock: &str, token: &str) -> [u8; MARKER_SIZE] {
    let inner = binary_marker("X");
    let mut m = [0xFF; MARKER_SIZE];
    m[..4].copy_from_slice(b"#DST");
    m[4..4 + 71].copy_from_slice(&inner[..71]); // BM3_, the block, ~|~
    let mut at = 75;
    m[at..at + 4].fill(b'_');
    at += 4;
    for part in [format!("#{stock}"), "______".to_string(), format!("#{token}")] {
        m[at..at + part.len()].copy_from_slice(part.as_bytes());
        at += part.len();
    }
    m[at] = 0;
    m
}

/// The S58 layout (G87, issue #175): BM3_, synthetic bytes without '#', the only '#' at byte 101, the tag, NUL.
fn hash_marker(tag: &str) -> [u8; MARKER_SIZE] {
    let mut m = [0u8; MARKER_SIZE];
    m[..4].copy_from_slice(b"BM3_");
    for (i, byte) in m.iter_mut().enumerate().take(101).skip(4) {
        let v = ((i * 37 + 11) & 0xFF) as u8;
        *byte = if v == 0x23 { 0x24 } else { v };
    }
    m[101] = b'#';
    m[102..102 + tag.len()].copy_from_slice(tag.as_bytes());
    m
}

fn rom_of(m: &[u8]) -> Option<&'static str> {
    marker_profile(m).ok().map(|p| p.rom)
}

/// Every check, as test_bm3.c makes them; the failures, each with its line here.
fn failures() -> Vec<String> {
    let mut failed = Vec::new();
    macro_rules! check {
        ($x:expr) => {
            if !$x {
                failed.push(format!("FAIL line {}: {}", line!(), stringify!($x)));
            }
        };
    }

    // S58 hash layout
    check!(rom_of(&hash_marker("F4C9L8R02GGGJM_G87_RWD_USA_KOR_ULEV246_")) == Some("F4C9L8R02"));
    check!(rom_of(&hash_marker("ZZZZZZZZZGGGJM_G87_RWD_USA_")).is_none());
    check!(rom_of(&hash_marker("F4C9L8R02")).is_none());
    let mut m = hash_marker("F4C9L8R02GGGJM_G87_RWD_USA_");
    m[50] = b'#';
    check!(rom_of(&m).is_none());

    // Captured binary layout
    let m = binary_marker("R4C9H5V02OKLX0");
    let p = marker_profile(&m).ok();
    check!(p.is_some_and(|p| p.rom == "R4C9H5V02" && p.map_address == 0x6003_8006 && p.write_service == 0x3E));
    let damaged = |at: usize, byte: u8| {
        let mut bad = m;
        bad[at] = byte;
        bad
    };
    check!(rom_of(&damaged(0, 0)).is_none()); // damaged prefix
    check!(rom_of(&damaged(68, 0)).is_none()); // damaged delimiter
    check!(rom_of(&damaged(4 + 64 + 3 + 30 + 1 + 14, b'_')).is_none()); // no terminator
    check!(rom_of(&binary_marker("UNKNOWN123OKLX0")).is_none());
    check!(rom_of(&binary_marker("R4C9H5V02")).is_none()); // no build characters
    check!(rom_of(&binary_marker("R4C9H5V02OKLX0EXTRA")).is_none());
    check!(rom_of(&m[..MARKER_SIZE - 1]).is_none()); // short
    check!(rom_of(&[0u8; MARKER_SIZE]).is_none()); // empty

    // F-series MEVD17 layout
    let m = mevd17_marker("EPST:R1C9J8B3B", "R1C900000CEKXA");
    let p = marker_profile(&m).ok();
    check!(p.is_some_and(|p| p.rom == "R1C900000" && p.map_address == 0x4005_E006 && p.write_service == 0x3E));
    check!(rom_of(&mevd17_marker("EPST:R1C9J8B3B", "UNKNOWN12CEKXA")).is_none());
    check!(rom_of(&mevd17_marker("EPST:R1C9J8B3B", "R1C900000")).is_none()); // no build characters
    check!(rom_of(&mevd17_marker("R1C900000CEKXA", "EPST:R1C9J8B3B")).is_none()); // the ROM isn't last
    let mut bad = m;
    bad[2] = b'X';
    check!(rom_of(&bad).is_none()); // damaged #DST
    let mut bad = m;
    bad[72] = 0;
    check!(rom_of(&bad).is_none()); // damaged delimiter

    // Every profile, in the legacy and binary layouts
    check!(PROFILES.len() == 150);
    for profile in PROFILES.iter() {
        let mut m = [0u8; MARKER_SIZE];
        let legacy = format!("BM3_R#{}#", profile.rom);
        m[..legacy.len()].copy_from_slice(legacy.as_bytes());
        if rom_of(&m) != Some(profile.rom) {
            failed.push(format!("legacy marker for {}", profile.rom));
        }
        if rom_of(&binary_marker(&format!("{}OKLX0", profile.rom))) != Some(profile.rom) {
            failed.push(format!("binary marker for {}", profile.rom));
        }
        check!(profile.write_service == 0x3D || profile.write_service == 0x3E);
    }

    // The user's ROM
    let p = profile_for(b"R1C900000");
    check!(p.is_some_and(|p| p.map_address == 0x4005_E006 && p.write_service == 0x3E));
    let Some(p) = p else {
        return failed;
    };

    // Requests and reply prefixes, as upstream asserts them
    let r = clear_dynamic_did();
    check!(r == [0x2C, 3, 0xF3, 1]);
    check!(reply_prefix(&r) == [0x6C, 0x03, 0xF3, 0x01]);
    let r = define_marker_did(0x807F_FE50);
    check!(r == [0x2C, 2, 0xF3, 1, 0x24, 0x80, 0x7F, 0xFE, 0x50, 0, 255]);
    check!(reply_prefix(&r) == [0x6C, 0x02, 0xF3, 0x01]);
    let r = read_marker_did();
    check!(r == [0x22, 0xF3, 1]);
    check!(reply_prefix(&r) == [0x62, 0xF3, 0x01]);
    let r = read_memory(0x6003_8006, 1);
    check!(r == [0x23, 0x14, 0x60, 3, 0x80, 6, 1]);
    check!(reply_prefix(&r) == [0x63]);
    check!(read_memory(0x807F_FE50, 255) == [0x23, 0x14, 0x80, 0x7F, 0xFE, 0x50, 255]);
    let slot_data = [3, 1];
    let r = write_memory(64, p, p.map_address, &slot_data);
    check!(r == [0x3E, 0x14, 0x40, 0x05, 0xE0, 0x06, 2, 3, 1]);
    check!(reply_prefix(&r) == [0x7E, 0x3D]);
    let mut d: Profile = *p;
    d.write_service = 0x3D;
    let r = write_memory(64, &d, d.map_address, &slot_data);
    check!(r.len() == 9 && r[0] == 0x3D && reply_prefix(&r) == [0x7D]);
    d.write_service = 0x27;
    check!(write_memory(64, &d, d.map_address, &slot_data).is_empty());
    check!(write_memory(8, p, p.map_address, &slot_data).is_empty()); // does not fit
    check!(slot_reply(&[0x63, 0x02]) == Some(2));
    check!(slot_reply(&[0x63, 0x04]).is_none());
    check!(slot_reply(&[0x63, 0x01, 0x00]).is_none());

    // Active Sound and burbles, as bm3_protocol.js sends them (setAsd, setBurbles)
    let r = sound_request(true);
    check!(r == [0x2E, 0x5F, 0xFD, 0x00]);
    check!(reply_prefix(&r) == [0x6E, 0x5F, 0xFD]);
    check!(sound_request(false) == [0x2E, 0x5F, 0xFD, 0x01]);
    // factor 2: aggression 4, 8 and 12 are -8, -16 and -24; off is 0
    check!(burble_table(p, 0) == Some([0; 24]));
    check!(burble_table(p, 1) == Some([0xF8; 24]));
    check!(burble_table(p, 2) == Some([0xF0; 24]));
    check!(burble_table(p, 3) == Some([0xE8; 24]));
    check!(burble_table(p, 4).is_none());
    let mut d: Profile = *p;
    d.factor = 11; // 12 * 11 > 127: the byte would wrap
    check!(burble_table(&d, 1).is_none());
    d.factor = 0;
    check!(burble_table(&d, 0).is_none());
    for profile in PROFILES.iter() {
        check!(burble_table(profile, BURBLE_PRESETS).is_some());
    }
    failed
}

fn main() {
    let failed = failures();
    for failure in &failed {
        eprintln!("{failure}");
    }
    if !failed.is_empty() {
        std::process::exit(1);
    }
    println!("BM3: marker layouts, all 150 profiles, requests, replies and burble tables passed.");
}

#[cfg(test)]
mod tests {
    #[test]
    fn every_check_of_test_bm3_c_passes() {
        assert_eq!(super::failures(), Vec::<String>::new());
    }
}

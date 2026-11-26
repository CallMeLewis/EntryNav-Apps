//! The gauge workers' side of src/gauge_core.c and src/drive_frame.h: what each of BoostDeck's readings means
//! (decode_did), the text frame the BoostDeck collector sends the app on UDP 42860 (encode), the binary frame
//! ShiftDeck's worker sends on UDP 42863 (DriveFrame), and the socket both send from. The apps' side (bd_parse,
//! bd_is_fresh, the display units and bars) stays in the C++ plugins.

use crate::sys;
use std::net::Ipv4Addr;
use std::os::fd::OwnedFd;

/// BoostDeck's readings, in the frame's order: psi, gasoline-equivalent AFR, degrees, C, C, bar, bar, C.
pub const COUNT: usize = 8;
pub const BOOST: usize = 0;
pub const RAIL: usize = 5;
pub const OIL: usize = 6;
pub const COOLANT: usize = 7;
/// Where BoostDeck's app listens (BD_UDP_PORT), and the most a frame may take with its newline and C's NUL.
pub const UDP_PORT: u16 = 42860;
pub const MAX_PACKET: usize = 512;
/// Where ShiftDeck's app listens (DRIVE_PORT).
pub const DRIVE_PORT: u16 = 42863;

/// A reading from its payload, the bytes after 62 and the DID, or None when the DID isn't one of BoostDeck's, the
/// payload has the wrong length, or the value is a sentinel or out of range (bd_decode_did; docs/SIGNALS.md has the
/// formulas' sources).
pub fn decode_did(did: u16, payload: &[u8]) -> Option<f64> {
    let width = if did == 0x580f { 1 } else { 2 };
    if payload.len() != width {
        return None;
    }
    let r = if width == 1 {
        u32::from(payload[0])
    } else {
        u32::from(payload[0]) << 8 | u32::from(payload[1])
    };
    let raw = f64::from(r);
    let (x, lo, hi) = match did {
        0x4201 => (raw / 256.0, 40.0, 115.0),       // ambient kPa
        0x4205 => (raw / 128.0, 20.0, 450.0),       // absolute kPa
        0x5889 => (raw * 16.0 / 65535.0, 0.5, 2.0), // lambda
        // This car returned FFFF, which can be -0.1 or "unavailable": no reading until that is checked. Other
        // negatives stay valid, FFFE (-0.2) among them.
        0x4a49 if r == 65535 => return None,
        0x4a49 => {
            let signed = if r & 0x8000 != 0 { r as i32 - 65536 } else { r as i32 };
            (f64::from(signed) / 10.0, -64.0, 64.0)
        }
        0x580f if r == 255 => return None,
        0x580f => (raw * 0.75 - 48.0, -48.0, 143.25),
        0xda12 => (raw - 48.0, -48.0, 180.0),
        0x56d7 | 0x586f | 0x4300 if r == 65535 => return None,
        0x56d7 => (raw / 50.0, 0.0, 500.0),  // rail bar
        0x586f => (raw / 1000.0, 0.0, 20.0), // oil bar
        0x4300 => (raw / 10.0 - 273.15, -48.0, 150.0),
        _ => return None,
    };
    (x.is_finite() && x >= lo && x <= hi).then_some(x)
}

/// printf's "%.6f": six decimals, rounded half to even on the exact binary value, as glibc and Rust both do, and the
/// words glibc uses for values that aren't numbers.
pub fn fixed6(x: f64) -> String {
    if x.is_nan() {
        return if x.is_sign_negative() { "-nan" } else { "nan" }.to_string();
    }
    if x.is_infinite() {
        return if x < 0.0 { "-inf" } else { "inf" }.to_string();
    }
    format!("{x:.6}")
}

/// One BoostDeck frame (bd_frame).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Frame {
    pub seq: u32,
    pub demo: u32,
    /// Bit i set when value[i] is a reading.
    pub valid: u32,
    /// The sender's CLOCK_MONOTONIC when it sent the frame.
    pub sent_ms: u64,
    pub value: [f64; COUNT],
    /// How old each reading is, at most 999999 ms.
    pub age_ms: [u32; COUNT],
}

/// The frame as the app reads it (bd_encode): "NBG3 seq demo valid sent_ms", then each value and its age, and a
/// newline. None when it wouldn't fit in MAX_PACKET with C's NUL.
pub fn encode(f: &Frame) -> Option<Vec<u8>> {
    let mut text = format!("NBG3 {} {} {} {}", f.seq, f.demo, f.valid, f.sent_ms);
    for (value, age) in f.value.iter().zip(f.age_ms) {
        text.push(' ');
        text.push_str(&fixed6(*value));
        text.push_str(&format!(" {age}"));
    }
    if text.len() > MAX_PACKET - 2 {
        return None;
    }
    text.push('\n');
    Some(text.into_bytes())
}

/// ShiftDeck's frame (struct drive_frame): the reading's time, engine speed in rpm and road speed in km/h, -1 for
/// either when the car didn't give it. 16 bytes in the head unit's byte order, little-endian, with no padding.
#[repr(C)]
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct DriveFrame {
    pub sampled_ms: u64,
    pub rpm: i32,
    pub kph: i32,
}

pub const DRIVE_FRAME_SIZE: usize = 16;
const _: () = assert!(std::mem::size_of::<DriveFrame>() == DRIVE_FRAME_SIZE);

impl DriveFrame {
    /// The bytes the app receives, laid out as the C struct is on the head unit, whatever the byte order of the
    /// machine running this.
    pub fn to_bytes(&self) -> [u8; DRIVE_FRAME_SIZE] {
        let mut out = [0u8; DRIVE_FRAME_SIZE];
        out[..8].copy_from_slice(&self.sampled_ms.to_le_bytes());
        out[8..12].copy_from_slice(&self.rpm.to_le_bytes());
        out[12..].copy_from_slice(&self.kph.to_le_bytes());
        out
    }
}

/// The UDP socket a gauge worker sends its frames from, to an app on 127.0.0.1. Only to the loopback address: the
/// frames never leave the head unit.
pub struct FrameSocket(OwnedFd);

impl FrameSocket {
    /// The socket, or the errno socket() failed with.
    pub fn new() -> Result<FrameSocket, i32> {
        sys::udp_socket().map(FrameSocket).ok_or_else(sys::errno)
    }

    /// sendto() to 127.0.0.1:port; the byte count, or -1 with errno.
    pub fn send(&self, data: &[u8], port: u16) -> isize {
        sys::send_to(&self.0, data, Ipv4Addr::LOCALHOST, port)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn check(did: u16, payload: &[u8], expected: f64) {
        let x = decode_did(did, payload).unwrap_or_else(|| panic!("{did:04X} {payload:02X?} gave no reading"));
        assert!((x - expected).abs() < 0.00001, "{did:04X}: {x}, not {expected}");
    }

    /// tests/test_core.c: payloads from OBDb's 2016 3-Series captures (CC BY-SA 4.0).
    const BARO: [u8; 2] = [0x63, 0xca];
    const PRESSURE: [u8; 2] = [0x31, 0xe5];
    const TIMING: [u8; 2] = [0, 0x4e];
    const IAT: [u8; 1] = [0x82];
    const LAMBDA: [u8; 2] = [0x10, 0x4e];
    const TRANS: [u8; 2] = [0, 0x8e];
    const INVALID: [u8; 2] = [0xff, 0xff];

    #[test]
    fn recorded_payloads() {
        check(0x4201, &BARO, 99.7890625);
        check(0x4205, &PRESSURE, 99.7890625);
        check(0x4a49, &TIMING, 7.8);
        check(0x580f, &IAT, 49.5);
        check(0x5889, &LAMBDA, 4174.0 * 16.0 / 65535.0);
        check(0xda12, &TRANS, 94.0);
        // tests/test_transport.py's rail, oil and coolant.
        check(0x56d7, &[0x11, 0x69], 89.14);
        check(0x586f, &[0x09, 0xc1], 2.497);
        check(0x4300, &[0x0b, 0xf8], 33.25);
    }

    #[test]
    fn signed_timing_and_its_sentinel() {
        check(0x4a49, &[0xff, 0xce], -5.0);
        check(0x4a49, &[0xff, 0xfe], -0.2);
        assert_eq!(decode_did(0x4a49, &INVALID), None);
        assert_eq!(decode_did(0x4a49, &[0x80, 0]), None); // -3276.8, out of range
    }

    #[test]
    fn sentinels_ranges_and_unknown_dids() {
        assert_eq!(decode_did(0x4205, &BARO[..1]), None);
        assert_eq!(decode_did(0xda12, &INVALID), None);
        assert_eq!(decode_did(0x5889, &INVALID), None);
        assert_eq!(decode_did(0xffff, &BARO), None);
        assert_eq!(decode_did(0x580f, &[0xff]), None);
        for did in [0x56d7, 0x586f, 0x4300] {
            assert_eq!(decode_did(did, &INVALID), None, "{did:04X}");
        }
        assert_eq!(decode_did(0x4201, &[0x27, 0xff]), None); // 39.996 kPa, under 40
        check(0x4201, &[0x28, 0x00], 40.0);
    }

    #[test]
    fn a_payload_one_byte_short_or_long_gives_no_reading() {
        let payloads: [(u16, &[u8]); 6] = [
            (0x4201, &BARO),
            (0x4205, &PRESSURE),
            (0x5889, &LAMBDA),
            (0x4a49, &TIMING),
            (0x580f, &IAT),
            (0xda12, &TRANS),
        ];
        for (did, payload) in payloads {
            assert_eq!(decode_did(did, &payload[..payload.len() - 1]), None, "{did:04X} short");
            let mut longer = payload.to_vec();
            longer.push(0xaa);
            assert_eq!(decode_did(did, &longer), None, "{did:04X} long");
        }
    }

    #[test]
    fn the_frame_as_the_app_reads_it() {
        let mut f = Frame {
            seq: 123,
            valid: 31,
            sent_ms: 1234567890123,
            ..Frame::default()
        };
        f.value[..5].copy_from_slice(&[18.25, 14.7, -5.0, 49.5, 94.0]);
        f.age_ms = [0, 1, 2, 3, 999999, 5, 6, 7];
        assert_eq!(
            String::from_utf8(encode(&f).unwrap()).unwrap(),
            "NBG3 123 0 31 1234567890123 18.250000 0 14.700000 1 -5.000000 2 49.500000 3 94.000000 999999 \
             0.000000 5 0.000000 6 0.000000 7\n"
        );
        let full = Frame {
            seq: u32::MAX,
            demo: 1,
            valid: 255,
            sent_ms: u64::MAX,
            ..Frame::default()
        };
        assert!(encode(&full)
            .unwrap()
            .starts_with(b"NBG3 4294967295 1 255 18446744073709551615 0.000000 0 "));
    }

    #[test]
    fn a_frame_too_long_for_the_packet_is_refused() {
        // bd_encode takes up to 510 characters before the newline: with it and C's NUL, 512.
        let mut f = Frame::default();
        f.value[0] = 1e200;
        f.value[1] = 1e200;
        let mut lengths = Vec::new();
        for k in 0..40 {
            f.value[2] = 10f64.powi(k);
            let length = "NBG3 0 0 0 0".len()
                + f.value.iter().map(|v| 1 + fixed6(*v).len() + 2).sum::<usize>();
            assert_eq!(encode(&f).is_some(), length <= 510, "{length} characters");
            if let Some(packet) = encode(&f) {
                assert_eq!(packet.len(), length + 1);
            }
            lengths.push(length);
        }
        assert!(lengths.contains(&510) && lengths.contains(&511));
    }

    #[test]
    fn six_decimals_as_the_c_library_prints_them() {
        let mut values = vec![
            0.0,
            -0.0,
            1.0,
            -1.0,
            0.5,
            2.5,
            0.0000005,
            0.0000015,
            0.0000025,
            -0.0000005,
            1e-7,
            -1e-7,
            0.1,
            0.2,
            0.3,
            14.7,
            99.7890625,
            1.0 / 3.0,
            123_456_789.123_456_79,
            1e15,
            1e22,
            4.35,
            2.675,
            1e300,
            -1e300,
            f64::MAX,
            f64::MIN_POSITIVE,
            5e-324,
        ];
        // Exact ties at the seventh decimal: k/128 and k/2^20 have short binary expansions.
        values.extend((0..4096).map(|k| f64::from(k) / 128.0 - 16.0));
        values.extend((0..4096).map(|k| f64::from(k) / 1048576.0));
        values.extend((0..4096).map(|k| f64::from(k) * 0.0000005));
        // Everything the decoders and the collector's scaling can give.
        for r in (0..=0xffffu32).step_by(7) {
            let raw = f64::from(r);
            values.extend([
                raw / 256.0,
                raw / 128.0,
                raw * 16.0 / 65535.0 * 14.7,
                raw / 10.0,
                raw / 50.0,
            ]);
            values.extend([
                raw / 1000.0,
                raw / 10.0 - 273.15,
                (raw / 128.0 - 99.7890625) * 0.145037738,
            ]);
        }
        let mut state = 0x2545f4914f6cdd1du64;
        for _ in 0..20000 {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
            let x = f64::from_bits(state);
            if x.is_finite() && x.abs() < 1e30 {
                values.push(x);
            }
            values.push((state >> 11) as f64 / (1u64 << 40) as f64 - 4096.0);
        }
        for x in values {
            assert_eq!(fixed6(x), sys::c_fixed6(x), "{x:e}");
        }
        assert_eq!(fixed6(f64::NAN), "nan");
        assert_eq!(fixed6(-f64::NAN), "-nan");
        assert_eq!(fixed6(f64::INFINITY), "inf");
        assert_eq!(fixed6(f64::NEG_INFINITY), "-inf");
    }

    #[test]
    fn the_drive_frame_is_the_c_struct() {
        assert_eq!(std::mem::offset_of!(DriveFrame, sampled_ms), 0);
        assert_eq!(std::mem::offset_of!(DriveFrame, rpm), 8);
        assert_eq!(std::mem::offset_of!(DriveFrame, kph), 12);
        let f = DriveFrame {
            sampled_ms: 0x0102030405060708,
            rpm: 6250,
            kph: -1,
        };
        assert_eq!(
            f.to_bytes(),
            [8, 7, 6, 5, 4, 3, 2, 1, 0x6a, 0x18, 0, 0, 0xff, 0xff, 0xff, 0xff]
        );
    }
}

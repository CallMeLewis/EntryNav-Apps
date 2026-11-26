//! Finding the diagnostic gateway on the head unit's car-side Ethernet (find_gateways, save_gateway and the
//! gateway_search functions of src/collector.c), and which addresses it can have (gateway_address_ok of
//! src/diagnostic_lease.h).
//!
//! HSFZ vehicle identification: 00 00 00 00 00 11 to UDP 6811 on the broadcast address of every IPv4 network of the
//! device. Each controller answers with its identification as text; the gateway's says DIAGADR10. The gateway is
//! usually link-local, but takes an address from tethering on the ENET cable while that hands them out.

use crate::{hsfz, mono_ms, sys, warn};
use std::ffi::OsStr;
use std::fs::File;
use std::io::Write;
use std::net::Ipv4Addr;
use std::os::unix::ffi::OsStrExt;

pub const MAX_NETWORKS: usize = 8;
pub const MAX_GATEWAYS: usize = 16;
pub const IDENTIFICATION_REQUEST: [u8; 6] = [0, 0, 0, 0, 0, 0x11];
/// Where setup.sh saves the gateway for every app (ENA_GATEWAY_FILE of src/paths.h).
pub const GATEWAY_FILE: &str = "/var/local/data/EntryNavApps/gateway-ip.txt";

/// Whether address (host order) can be the gateway: link-local 169.254.1.0 to 169.254.254.255, or private (10/8,
/// 172.16/12, 192.168/16) when tethering handed it out. setup.sh and Flare Drive use the same rule.
pub fn gateway_address_ok(address: u32) -> bool {
    if address >> 16 == 0xa9fe {
        let third = address >> 8 & 0xff;
        return (1..=254).contains(&third);
    }
    address >> 24 == 10 || address >> 20 == 0xac1 || address >> 16 == 0xc0a8
}

/// Whether an identification answer is the gateway's: control 0x11, and DIAGADR10 somewhere after the header.
pub fn is_gateway_identification(reply: &[u8]) -> bool {
    reply.len() >= 6 && reply[4] == 0 && reply[5] == 0x11 && reply[6..].windows(9).any(|w| w == b"DIAGADR10")
}

/// The device's IPv4 networks (address, mask): the interface and its labelled aliases (vbr0:1), not its VLANs
/// (vbr0.73). At most MAX_NETWORKS. getifaddrs failing is reported as perror does and gives none.
fn device_networks(device: &[u8]) -> Vec<(u32, u32)> {
    match sys::interface_addresses() {
        Err(errno) => {
            warn(format!("getifaddrs: {}\n", crate::strerror(errno)).as_bytes());
            Vec::new()
        }
        Ok(list) => list
            .into_iter()
            .filter(|i| i.name.starts_with(device) && (i.name.len() == device.len() || i.name[device.len()] == b':'))
            .map(|i| (i.address, i.mask))
            .take(MAX_NETWORKS)
            .collect(),
    }
}

/// Why no search could run.
#[derive(Debug, PartialEq, Eq)]
pub enum SearchError {
    /// The device has no IPv4 address (find_gateways' -1).
    NoAddress,
    /// Nothing could be sent (-2).
    NotSent,
}

/// Asks every network on device, or only `to` (which accepts any responder, for the tests), and collects each
/// different responder that calls itself the gateway within wait_ms, up to capacity, in the order they answered.
pub fn find_gateways(
    device: &[u8],
    to: Option<&[u8]>,
    port: u16,
    wait_ms: u64,
    capacity: usize,
) -> Result<Vec<Ipv4Addr>, SearchError> {
    let mut destinations = Vec::new();
    let mut networks = Vec::new();
    match to {
        Some(text) => match crate::ipv4(text) {
            Some(address) => destinations.push(address),
            None => {
                let mut message = b"Invalid address: ".to_vec();
                message.extend_from_slice(text);
                message.push(b'\n');
                warn(&message);
                return Err(SearchError::NotSent);
            }
        },
        None => {
            networks = device_networks(device);
            if networks.is_empty() {
                let mut message = device.to_vec();
                message.extend_from_slice(b" has no IPv4 address\n");
                warn(&message);
                return Err(SearchError::NoAddress);
            }
            destinations.extend(networks.iter().map(|&(address, mask)| Ipv4Addr::from(address | !mask)));
        }
    }
    let Some(fd) = sys::udp_socket() else {
        warn(format!("socket: {}\n", crate::strerror(sys::errno())).as_bytes());
        return Err(SearchError::NotSent);
    };
    sys::set_broadcast(&fd);
    let mut sent = 0;
    for destination in destinations {
        if sys::send_to(&fd, &IDENTIFICATION_REQUEST, destination, port) < 0 {
            warn(format!("sendto {destination}: {}\n", crate::strerror(sys::errno())).as_bytes());
        } else {
            sent += 1;
        }
    }
    if sent == 0 {
        return Err(SearchError::NotSent);
    }
    let deadline = mono_ms() + wait_ms;
    let mut found: Vec<Ipv4Addr> = Vec::new();
    let mut reply = [0u8; 2048];
    while crate::running() && found.len() < capacity && hsfz::ready(&fd, sys::POLLIN, deadline) {
        let (n, from) = sys::recv_from(&fd, &mut reply[..2047]);
        if n < 6 {
            continue;
        }
        let host = u32::from(from);
        if to.is_none() && !networks.iter().any(|&(address, mask)| host & mask == address & mask) {
            continue;
        }
        if is_gateway_identification(&reply[..n as usize]) && !found.contains(&from) {
            found.push(from);
        }
    }
    Ok(found)
}

/// Saves address for every app, as setup.sh does: written whole to path.new, flushed to disk, then renamed into place.
pub fn save_gateway(path: &[u8], address: &str) -> bool {
    if path.len() + 4 >= 256 {
        return false;
    }
    let mut temporary = path.to_vec();
    temporary.extend_from_slice(b".new");
    let temporary = OsStr::from_bytes(&temporary);
    let Ok(mut file) = File::create(temporary) else {
        return false;
    };
    let written = file.write_all(format!("{address}\n").as_bytes()).is_ok() && file.sync_all().is_ok();
    drop(file);
    if !written || std::fs::rename(temporary, OsStr::from_bytes(path)).is_err() {
        let _ = std::fs::remove_file(temporary);
        return false;
    }
    true
}

pub const SEARCH_FAILURES: u32 = 3;
pub const SEARCH_EVERY_MS: u64 = 20000;
pub const SEARCH_WAIT_MS: u64 = 1200;

/// A worker's search for the gateway when the saved address stops answering. --search-dev, --search-port and
/// --gateway-file change where it looks and saves, for the tests.
pub struct GatewaySearch {
    pub device: Vec<u8>,
    pub file: Vec<u8>,
    pub port: u16,
    pub failures: u32,
    pub next: u64,
}

impl Default for GatewaySearch {
    fn default() -> Self {
        GatewaySearch {
            device: b"vbr0".to_vec(),
            file: GATEWAY_FILE.as_bytes().to_vec(),
            port: 6811,
            failures: 0,
            next: 0,
        }
    }
}

impl GatewaySearch {
    /// Takes --search-dev, --search-port or --gateway-file at args[*i] and its value; true if it was one of them.
    pub fn option(&mut self, args: &[Vec<u8>], i: &mut usize) -> bool {
        if *i + 1 >= args.len() {
            return false;
        }
        match args[*i].as_slice() {
            b"--search-dev" => self.device = args[*i + 1].clone(),
            b"--search-port" => self.port = crate::number(&args[*i + 1], 1, 65535) as u16,
            b"--gateway-file" => self.file = args[*i + 1].clone(),
            _ => return false,
        }
        *i += 1;
        true
    }

    /// Called after each failed connection to host. After SEARCH_FAILURES in a row, and at most every SEARCH_EVERY_MS,
    /// it asks the device for the gateway. The first other address that takes a connection on hsfz_port replaces host
    /// and is saved for the other apps. True when host changed.
    pub fn after_failure(&mut self, host: &mut Vec<u8>, hsfz_port: u16) -> bool {
        let now = mono_ms();
        self.failures += 1;
        if self.failures < SEARCH_FAILURES || now < self.next {
            return false;
        }
        self.next = now + SEARCH_EVERY_MS;
        let found = find_gateways(&self.device, None, self.port, SEARCH_WAIT_MS, MAX_GATEWAYS).unwrap_or_default();
        for candidate in found {
            let text = candidate.to_string();
            if text.as_bytes() == host.as_slice() {
                continue;
            }
            // The tests search lo, where every address is loopback.
            let address = u32::from(candidate);
            let test_loopback = self.device == b"lo" && address >> 24 == 127;
            if !gateway_address_ok(address) && !test_loopback {
                continue;
            }
            if hsfz::connect_gw(text.as_bytes(), hsfz_port).is_none() {
                continue;
            }
            let mut message = b"The gateway moved from ".to_vec();
            message.extend_from_slice(host);
            message.extend_from_slice(format!(" to {text}\n").as_bytes());
            warn(&message);
            *host = text.clone().into_bytes();
            save_gateway(&self.file, &text);
            self.failures = 0;
            return true;
        }
        false
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::UdpSocket;
    use std::thread;
    use std::time::Duration;

    fn address(text: &str) -> u32 {
        u32::from(text.parse::<Ipv4Addr>().unwrap())
    }

    #[test]
    fn the_gateway_is_link_local_or_private() {
        for good in [
            "169.254.10.3",
            "169.254.1.0",
            "169.254.254.255",
            "10.0.0.1",
            "172.16.0.1",
            "172.31.255.254",
            "192.168.43.12",
        ] {
            assert!(gateway_address_ok(address(good)), "{good}");
        }
        // setup.sh's test_setup.py refuses the same ones, and the edges of the link-local range.
        for bad in [
            "160.48.199.97",
            "8.8.8.8",
            "172.32.0.1",
            "169.254.0.5",
            "169.254.255.1",
            "127.0.0.1",
            "172.15.255.255",
            "192.169.0.1",
            "0.0.0.0",
        ] {
            assert!(!gateway_address_ok(address(bad)), "{bad}");
        }
    }

    fn answer(control: u8, text: &[u8]) -> Vec<u8> {
        let mut out = (text.len() as u32).to_be_bytes().to_vec();
        out.extend_from_slice(&[0, control]);
        out.extend_from_slice(text);
        out
    }

    #[test]
    fn only_the_gateways_identification_counts() {
        assert!(is_gateway_identification(&answer(
            0x11,
            b"DIAGADR10BMWMAC001122334455BMWVINWBA0"
        )));
        assert!(is_gateway_identification(&answer(0x11, b"BMWMAC001122334455DIAGADR10")));
        assert!(!is_gateway_identification(&answer(
            0x11,
            b"DIAGADR62BMWMAC001122334455"
        )));
        assert!(!is_gateway_identification(&answer(
            0x12,
            b"DIAGADR10BMWMAC001122334455"
        )));
        assert!(!is_gateway_identification(&answer(0x11, b"DIAGADR1")));
        assert!(!is_gateway_identification(&[0, 0x11]));
    }

    fn responder(replies: Vec<Vec<u8>>) -> (u16, thread::JoinHandle<Vec<u8>>) {
        let socket = UdpSocket::bind("127.0.0.1:0").unwrap();
        socket.set_read_timeout(Some(Duration::from_secs(3))).unwrap();
        let port = socket.local_addr().unwrap().port();
        let handle = thread::spawn(move || {
            let mut request = [0u8; 64];
            let (n, sender) = socket.recv_from(&mut request).unwrap();
            for reply in replies {
                socket.send_to(&reply, sender).unwrap();
            }
            request[..n].to_vec()
        });
        (port, handle)
    }

    #[test]
    fn finds_the_gateway_once_and_skips_the_rest() {
        let gateway = answer(0x11, b"DIAGADR10BMWMAC001122334455");
        let (port, handle) = responder(vec![
            answer(0x11, b"DIAGADR62"),
            gateway.clone(),
            gateway,
            vec![0, 0x11],
        ]);
        let found = find_gateways(b"vbr0", Some(b"127.0.0.1"), port, 400, MAX_GATEWAYS).unwrap();
        assert_eq!(handle.join().unwrap(), IDENTIFICATION_REQUEST);
        assert_eq!(found, vec![Ipv4Addr::LOCALHOST]);
    }

    #[test]
    fn a_device_without_an_address_and_a_bad_destination() {
        assert_eq!(
            find_gateways(b"nonexistent0", None, 6811, 100, MAX_GATEWAYS),
            Err(SearchError::NoAddress)
        );
        assert_eq!(
            find_gateways(b"", None, 6811, 100, MAX_GATEWAYS),
            Err(SearchError::NoAddress)
        );
        assert_eq!(
            find_gateways(b"lo", Some(b"1.2.3"), 6811, 100, MAX_GATEWAYS),
            Err(SearchError::NotSent)
        );
    }

    #[test]
    fn saves_whole_and_leaves_no_temporary_file() {
        let folder = std::env::temp_dir().join(format!("ena-diag-save-{}", std::process::id()));
        std::fs::create_dir_all(&folder).unwrap();
        let path = folder.join("gateway-ip.txt");
        assert!(save_gateway(path.as_os_str().as_bytes(), "169.254.10.3"));
        assert_eq!(std::fs::read_to_string(&path).unwrap(), "169.254.10.3\n");
        assert!(!folder.join("gateway-ip.txt.new").exists());
        assert!(!save_gateway(
            folder.join("missing/gateway-ip.txt").as_os_str().as_bytes(),
            "10.0.0.1"
        ));
        assert!(!save_gateway(&[b'a'; 252], "10.0.0.1")); // path.new would not fit in 256 bytes
        std::fs::remove_dir_all(&folder).unwrap();
    }

    #[test]
    fn searches_only_after_three_failures_in_a_row() {
        let mut search = GatewaySearch {
            device: b"nonexistent0".to_vec(),
            ..GatewaySearch::default()
        };
        let mut host = b"169.254.10.3".to_vec();
        assert!(!search.after_failure(&mut host, 6801));
        assert!(!search.after_failure(&mut host, 6801));
        assert_eq!(search.next, 0);
        assert!(!search.after_failure(&mut host, 6801)); // searched: no address on the device
        assert!(search.next > 0);
        assert_eq!(host, b"169.254.10.3");
    }
}

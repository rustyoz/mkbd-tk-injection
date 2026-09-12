//! bdaddr string<->bytes helpers (lib/mkbd_common.py bdaddr_to_bytes / bytes_to_bdaddr).

pub fn bdaddr_to_bytes(s: &str, little_endian: bool) -> Result<[u8; 6], String> {
    let parts: Vec<&str> = s.trim().split(['-', ':']).collect();
    if parts.len() != 6 {
        return Err(format!("bad bdaddr: {s:?}"));
    }
    let mut b = [0u8; 6];
    for (i, p) in parts.iter().enumerate() {
        b[i] = u8::from_str_radix(p, 16).map_err(|_| format!("bad bdaddr: {s:?}"))?;
    }
    if little_endian {
        b.reverse();
    }
    Ok(b)
}

pub fn bytes_to_bdaddr(b: &[u8], little_endian: bool) -> String {
    let mut v = b.to_vec();
    if little_endian {
        v.reverse();
    }
    v.iter()
        .map(|x| format!("{x:02X}"))
        .collect::<Vec<_>>()
        .join(":")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip() {
        let s = "AA:BB:CC:DD:EE:FF";
        let le = bdaddr_to_bytes(s, true).unwrap();
        assert_eq!(le, [0xFF, 0xEE, 0xDD, 0xCC, 0xBB, 0xAA]);
        assert_eq!(bytes_to_bdaddr(&le, true), s);

        let be = bdaddr_to_bytes(s, false).unwrap();
        assert_eq!(be, [0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF]);
        assert_eq!(bytes_to_bdaddr(&be, false), s);
    }

    #[test]
    fn accepts_dash_separator() {
        assert_eq!(
            bdaddr_to_bytes("aa-bb-cc-dd-ee-ff", false).unwrap(),
            [0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF]
        );
    }
}

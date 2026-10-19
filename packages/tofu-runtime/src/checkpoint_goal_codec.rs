//! Bounded canonical Goal authority JSON, independent of provider map order.
//! Request/context digests retain the native sorted-object identity in every
//! runtime feature mode; provider payload values are never modified.
use crate::checkpoint_journal::Error;
use serde::{Serialize,Serializer,ser::{SerializeMap,SerializeSeq}};
use serde_json::Value;
use sha2::{Digest,Sha256};
use std::io::Write;
struct Bounded {bytes:Vec<u8>,maximum:usize}
impl Write for Bounded {
    fn write(&mut self,bytes:&[u8])->std::io::Result<usize>{
        if bytes.len()>self.maximum-self.bytes.len(){return Err(std::io::Error::other("Goal evidence bound"));}
        self.bytes.try_reserve(bytes.len()).map_err(std::io::Error::other)?;
        self.bytes.extend_from_slice(bytes);Ok(bytes.len())
    }
    fn flush(&mut self)->std::io::Result<()>{Ok(())}
}
pub fn encode(value:&impl Serialize,maximum:usize)->Result<Vec<u8>,Error>{
    let mut output=Bounded{bytes:Vec::new(),maximum};
    serde_json::to_writer(&mut output,value).map_err(|_|Error::Oversize)?;Ok(output.bytes)
}
struct Canonical<'a>(&'a Value);
impl Serialize for Canonical<'_>{
    fn serialize<S:Serializer>(&self,serializer:S)->Result<S::Ok,S::Error>{
        match self.0 {
            Value::Object(fields)=>{
                let mut output=serializer.serialize_map(Some(fields.len()))?;
                #[cfg(feature="provider-json-order")]
                {
                    let mut keys:Vec<_>=fields.keys().collect();keys.sort_unstable();
                    for key in keys {output.serialize_entry(key,&Canonical(&fields[key]))?;}
                }
                #[cfg(not(feature="provider-json-order"))]
                for (key,value) in fields {output.serialize_entry(key,&Canonical(value))?;}
                output.end()
            },
            Value::Array(values)=>{
                let mut output=serializer.serialize_seq(Some(values.len()))?;
                for value in values {output.serialize_element(&Canonical(value))?;}output.end()
            },value=>value.serialize(serializer),
        }
    }
}
pub fn digest(value:&Value,maximum:usize)->Result<String,Error>{
    Ok(format!("{:x}",Sha256::digest(encode(&Canonical(value),maximum)?)))
}
pub fn charge(value:&Value,remaining:&mut usize)->Result<(),Error>{
    struct Counter{used:usize,maximum:usize}
    impl Write for Counter{
        fn write(&mut self,bytes:&[u8])->std::io::Result<usize>{
            self.used=self.used.checked_add(bytes.len()).filter(|n|*n<=self.maximum)
                .ok_or_else(||std::io::Error::other("Goal evidence bound"))?;Ok(bytes.len())
        }
        fn flush(&mut self)->std::io::Result<()>{Ok(())}
    }
    let mut counter=Counter{used:0,maximum:*remaining};
    serde_json::to_writer(&mut counter,value).map_err(|_|Error::Oversize)?;
    *remaining-=counter.used;Ok(())
}
pub fn text<'a>(value:&'a Value,key:&str)->Result<&'a str,Error>{
    value[key].as_str().filter(|s|!s.is_empty() && s.chars().count()<=512).ok_or(Error::Invalid)
}
pub fn read_error(error:std::io::Error)->Error{
    match error.kind(){
        std::io::ErrorKind::WouldBlock|std::io::ErrorKind::AlreadyExists=>Error::Conflict,
        std::io::ErrorKind::InvalidInput|std::io::ErrorKind::InvalidData|std::io::ErrorKind::PermissionDenied=>Error::Invalid,
        _=>Error::Storage,
    }
}
#[cfg(test)]
mod tests{
    use super::*;
    #[test]
    fn authority_digest_and_charging_do_not_depend_on_provider_object_order(){
        let a:Value=serde_json::from_str(r#"{"z":[{"b":2,"a":"中文"}],"a":1}"#).unwrap();
        let b:Value=serde_json::from_str(r#"{"a":1,"z":[{"a":"中文","b":2}]}"#).unwrap();
        let canonical=r#"{"a":1,"z":[{"a":"中文","b":2}]}"#.as_bytes();
        let expected=format!("{:x}",Sha256::digest(canonical));
        assert_eq!(digest(&a,canonical.len()).unwrap(),expected);
        assert_eq!(digest(&b,canonical.len()).unwrap(),expected);
        assert_eq!(digest(&a,canonical.len()-1),Err(Error::Oversize));
        let mut exact=canonical.len();charge(&a,&mut exact).unwrap();assert_eq!(exact,0);
        let mut short=canonical.len()-1;assert_eq!(charge(&a,&mut short),Err(Error::Oversize));assert_eq!(short,canonical.len()-1);
        assert_eq!(a["z"][0]["b"],2);
    }
}

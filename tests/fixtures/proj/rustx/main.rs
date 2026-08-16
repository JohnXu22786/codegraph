mod lib;

fn main() {
    let p = lib::Point::origin();
    println!("{}", lib::dist(&p));
}

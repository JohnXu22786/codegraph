use std::fmt;

pub struct Point {
    pub x: i32,
    pub y: i32,
}

pub fn dist(p: &Point) -> f64 {
    let dx = p.x as f64;
    (dx * dx).sqrt()
}

impl Point {
    pub fn origin() -> Point {
        Point { x: 0, y: 0 }
    }
}

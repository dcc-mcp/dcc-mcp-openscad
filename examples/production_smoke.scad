// Deterministic production smoke for dcc-mcp-openscad.
width = 84;
depth = 56;
height = 26;
wall = 3;
corner_radius = 5;
vent_count = 5;

module rounded_plate(size_x, size_y, size_z, radius) {
    linear_extrude(height=size_z)
        offset(r=radius)
            square([size_x - 2 * radius, size_y - 2 * radius], center=true);
}

module enclosure_shell() {
    difference() {
        rounded_plate(width, depth, height, corner_radius);
        translate([0, 0, wall])
            rounded_plate(
                width - 2 * wall,
                depth - 2 * wall,
                height,
                max(1, corner_radius - wall)
            );
        for (index = [0 : vent_count - 1]) {
            translate([(index - (vent_count - 1) / 2) * 10, 0, height - wall])
                cube([5, depth - 16, wall * 3], center=true);
        }
        translate([width / 2 - wall, 0, height / 2])
            rotate([0, 90, 0])
                cylinder(h=wall * 3, r=4, center=true, $fn=32);
    }
}

enclosure_shell();

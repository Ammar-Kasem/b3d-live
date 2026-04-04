from build123d import *
from viewer import show

# --- 1. Revolved vase profile ---
with BuildPart() as vase:
    with BuildSketch(Plane.XZ) as sk:
        with BuildLine() as profile:
            Spline(
                (0, 0),
                (15, 5),
                (10, 20),
                (18, 35),
                (14, 50),
                tangents=((0, 1), (0, 1)),
            )
            Line((14, 50), (0, 50))
            Line((0, 50), (0, 0))
        make_face()
    revolve(axis=Axis.Z)

# --- 2. Swept pipe along a helix ---
with BuildPart() as helix_pipe:
    helix_path = Helix(pitch=8, height=40, radius=12)
    with BuildSketch(
        Plane(origin=helix_path @ 0, z_dir=helix_path % 0)
    ) as pipe_section:
        Circle(2)
    sweep(path=helix_path)

# --- 3. Lofted transition solid ---
with BuildPart() as loft_solid:
    with BuildSketch(Plane.XY.offset(0)) as s1:
        Rectangle(30, 30)
    with BuildSketch(Plane.XY.offset(20)) as s2:
        Circle(18)
    with BuildSketch(Plane.XY.offset(40)) as s3:
        RegularPolygon(12, 6)
    loft()

# --- 4. Gear-like toothed cylinder ---
with BuildPart() as gear:
    Cylinder(20, 10)
    with PolarLocations(20, 12):
        Cylinder(4, 10)
    Cylinder(8, 12, mode=Mode.SUBTRACT)

# --- 5. Filleted mounting bracket with counterbore and through-holes ---
with BuildPart() as bracket:
    Box(60, 40, 8)
    fillet(bracket.edges().filter_by(Axis.Z), radius=5)
    Cylinder(9, 3, mode=Mode.SUBTRACT)
    Cylinder(5, 8, mode=Mode.SUBTRACT)
    with GridLocations(36, 20, 2, 2):
        Cylinder(2.5, 8, mode=Mode.SUBTRACT)

show(vase.part, helix_pipe.part, loft_solid.part, gear.part, bracket.part)

from build123d import *
from body import body_color


with BuildPart() as cab:
    # cap_color = Color(0x4683CE)
    cap_color = body_color
    with BuildSketch() as cab_plan:
        RectangleRounded(16, 16, 1)
        split(bisect_by=Plane.YZ)

    extrude(amount=7, dir=(0, 0.15, 1))
    fillet(cab.edges().group_by(Axis.Z)[-1].group_by(Axis.X)[1:], 1)

    with BuildSketch(Plane.XZ.shift_origin((0, 0, 3))) as rear_window:
        RectangleRounded(8, 4, 0.75)
    extrude(amount=10, mode=Mode.SUBTRACT)

    with BuildSketch(Plane.XZ) as front_window:
        RectangleRounded(15.2, 11, 0.75)
    extrude(amount=-10, mode=Mode.SUBTRACT)

    with BuildSketch(Plane.YZ) as side_window:
        with Locations((3.5, 0)):
            with GridLocations(10, 0, 2, 1):
                Trapezoid(9, 5.5, 80, 100, align=(Align.CENTER, Align.MIN))
                fillet(side_window.vertices().group_by(Axis.Y)[-1], 0.5)
    extrude(amount=12, both=True, mode=Mode.SUBTRACT)

    mirror(about=Plane.YZ)

    cab.part.move(Location((0, -8, 8.5)))
    cab.part.color = cap_color

from build123d import *

body_color = Color(0x4683CE)

with BuildPart() as body:
    with BuildSketch() as body_skt:
        Rectangle(20, 35)
        fillet(body_skt.vertices(), 1)

    extrude(amount=10, taper=4)
    extrude(body_skt.sketch, amount=8, taper=2)

    with BuildSketch(Plane.YZ) as fender:
        Trapezoid(18, 6, 80, 88, align=Align.MIN)
        fillet(fender.vertices().group_by(Axis.Y)[-1], 1.5)

    extrude(amount=10.5, both=True)

    with BuildSketch(Plane.YZ.shift_origin((0, 3.5, 0))) as wheel_well:
        Trapezoid(12, 4, 70, 85, align=Align.MIN)
        fillet(wheel_well.vertices().group_by(Axis.Y)[-1], 2)

    extrude(amount=10.5, both=True, mode=Mode.SUBTRACT)

    fillet(body.edges().group_by(Axis.Z)[-1], 1)

    body_edges = body.edges().group_by(Axis.Z)[-6]
    fillet(body_edges, 0.1)

    fender_edges = (
        body.edges().group_by(Axis.X)[0] + body.edges().group_by(Axis.X)[-1]
    )
    fender_edges = fender_edges.group_by(Axis.Z)[1:]
    fillet(fender_edges, 0.4)

    with BuildSketch(
        Plane.XZ.offset(-body.vertices().sort_by(Axis.Y)[-1].Y - 0.5)
    ) as grill:
        Rectangle(16, 8.5, align=(Align.CENTER, Align.MIN))
        fillet(grill.vertices().group_by(Axis.Y)[-1], 1)

        with Locations((0, 6.5)):
            with GridLocations(12, 0, 2, 1):
                Circle(1, mode=Mode.SUBTRACT)

        with Locations((0, 3)):
            with GridLocations(0, 0.8, 1, 4):
                SlotOverall(10, 0.5, mode=Mode.SUBTRACT)

    extrude(amount=2)

    grill_perimeter = body.faces().sort_by(Axis.Y)[-1].outer_wire()
    fillet(grill_perimeter.edges(), 0.2)

    with BuildPart() as bumper:
        front_cnt = body.edges().group_by(Axis.Z)[0].sort_by(Axis.Y)[
            -1
        ] @ 0.5 - (0, 3)

        with BuildSketch() as bumper_plan:
            with BuildLine():
                EllipticalCenterArc(
                    front_cnt, 20, 4, start_angle=60, end_angle=120
                )
                offset(amount=1)
            make_face()

        extrude(amount=1, both=True)
        fillet(bumper.edges(), 0.25)

    body.part.color = body_color

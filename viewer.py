"""Standalone one-shot VTK window viewer for build123d shapes."""
import os
import tempfile

import pyvista as pv
from build123d import export_stl


def show(*shapes, title="build123d", **kwargs):
    """Display one or more build123d shapes in a VTK window."""
    pl = pv.Plotter(title=title)
    for shape in shapes:
        tmp = tempfile.mktemp(suffix=".stl")
        try:
            export_stl(shape, tmp)
            mesh = pv.read(tmp)
            pl.add_mesh(mesh, color=kwargs.pop("color", "#aec6e8"), **kwargs)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    pl.show()

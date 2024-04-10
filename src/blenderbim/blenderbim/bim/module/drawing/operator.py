# BlenderBIM Add-on - OpenBIM Blender Add-on
# Copyright (C) 2020, 2021 Dion Moult <dion@thinkmoult.com>
#
# This file is part of BlenderBIM Add-on.
#
# BlenderBIM Add-on is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# BlenderBIM Add-on is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with BlenderBIM Add-on.  If not, see <http://www.gnu.org/licenses/>.

import os
import re
import bpy
import json
import time
import bmesh
import shutil
import hashlib
import shapely
import subprocess
import numpy as np
import multiprocessing
import ifcopenshell
import ifcopenshell.geom
import ifcopenshell.util.selector
import ifcopenshell.util.representation
import ifcopenshell.util.element
import blenderbim.bim.schema
import blenderbim.tool as tool
import blenderbim.core.drawing as core
import blenderbim.bim.module.drawing.svgwriter as svgwriter
import blenderbim.bim.module.drawing.annotation as annotation
import blenderbim.bim.module.drawing.sheeter as sheeter
import blenderbim.bim.module.drawing.scheduler as scheduler
import blenderbim.bim.module.drawing.helper as helper
import blenderbim.bim.export_ifc
from blenderbim.bim.module.drawing.decoration import CutDecorator
from blenderbim.bim.module.drawing.data import DecoratorData, DrawingsData
from typing import NamedTuple, List, Union, Optional
from lxml import etree
from mathutils import Vector, Color, Matrix
from timeit import default_timer as timer
from blenderbim.bim.module.drawing.prop import RasterStyleProperty, Literal, RASTER_STYLE_PROPERTIES_EXCLUDE
from blenderbim.bim.ifc import IfcStore
from pathlib import Path
from bpy_extras.image_utils import load_image

cwd = os.path.dirname(os.path.realpath(__file__))


class profile:
    """
    A python context manager timing utility
    """

    def __init__(self, task):
        self.task = task

    def __enter__(self):
        self.start = timer()

    def __exit__(self, *args):
        print(self.task, timer() - self.start)


class Operator:
    def execute(self, context):
        IfcStore.execute_ifc_operator(self, context)
        blenderbim.bim.handler.refresh_ui_data()
        return {"FINISHED"}


class LineworkContexts(NamedTuple):
    body: List[List[int]]
    annotation: List[List[int]]


class AddAnnotationType(bpy.types.Operator, Operator):
    bl_idname = "bim.add_annotation_type"
    bl_label = "Add Annotation Type"
    bl_options = {"REGISTER", "UNDO"}

    def _execute(self, context):
        props = context.scene.BIMAnnotationProperties
        object_type = props.object_type
        has_representation = props.create_representation_for_type
        drawing = tool.Ifc.get_entity(bpy.context.scene.camera)

        if props.create_representation_for_type:
            obj = tool.Drawing.create_annotation_object(drawing, object_type)
        else:
            obj = bpy.data.objects.new(object_type, None)

        obj.name = props.type_name
        element = tool.Drawing.run_root_assign_class(
            obj=obj,
            ifc_class="IfcTypeProduct",
            predefined_type=object_type,
            should_add_representation=has_representation,
            context=ifcopenshell.util.representation.get_context(tool.Ifc.get(), "Model", "Annotation", "MODEL_VIEW"),
            ifc_representation_class=tool.Drawing.get_ifc_representation_class(object_type),
        )
        element.ApplicableOccurrence = f"IfcAnnotation/{object_type}"

        if props.create_representation_for_type and object_type == "IMAGE":
            bpy.ops.bim.add_reference_image("INVOKE_DEFAULT", use_existing_object_by_name=obj.name)


class EnableAddAnnotationType(bpy.types.Operator, Operator):
    bl_idname = "bim.enable_add_annotation_type"
    bl_label = "Enable Add Annotation Type"
    bl_options = {"REGISTER", "UNDO"}

    def _execute(self, context):
        bpy.context.scene.BIMAnnotationProperties.is_adding_type = True


class DisableAddAnnotationType(bpy.types.Operator, Operator):
    bl_idname = "bim.disable_add_annotation_type"
    bl_label = "Disable Add Annotation Type"
    bl_options = {"REGISTER", "UNDO"}

    def _execute(self, context):
        bpy.context.scene.BIMAnnotationProperties.is_adding_type = False


class AddDrawing(bpy.types.Operator, Operator):
    bl_idname = "bim.add_drawing"
    bl_label = "Add Drawing"
    bl_options = {"REGISTER", "UNDO"}

    def _execute(self, context):
        self.props = context.scene.DocProperties
        hint = self.props.location_hint
        if self.props.target_view in ["PLAN_VIEW", "REFLECTED_PLAN_VIEW"]:
            hint = int(hint)
        core.add_drawing(
            tool.Ifc,
            tool.Collector,
            tool.Drawing,
            target_view=self.props.target_view,
            location_hint=hint,
        )
        try:
            drawing = tool.Ifc.get().by_id(self.props.active_drawing_id)
            core.sync_references(tool.Ifc, tool.Collector, tool.Drawing, drawing=drawing)
        except:
            pass


class DuplicateDrawing(bpy.types.Operator, Operator):
    bl_idname = "bim.duplicate_drawing"
    bl_label = "Duplicate Drawing"
    bl_options = {"REGISTER", "UNDO"}
    drawing: bpy.props.IntProperty()
    should_duplicate_annotations: bpy.props.BoolProperty(name="Should Duplicate Annotations", default=False)

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        row = self.layout
        row.prop(self, "should_duplicate_annotations")

    def _execute(self, context):
        self.props = context.scene.DocProperties
        core.duplicate_drawing(
            tool.Ifc,
            tool.Drawing,
            drawing=tool.Ifc.get().by_id(self.drawing),
            should_duplicate_annotations=self.should_duplicate_annotations,
        )
        try:
            drawing = tool.Ifc.get().by_id(self.props.active_drawing_id)
            core.sync_references(tool.Ifc, tool.Collector, tool.Drawing, drawing=drawing)
        except:
            pass


class CreateDrawing(bpy.types.Operator):
    """Creates/refreshes a .svg drawing

    Only available if :
    - IFC file is created
    - Camera is in Orthographic mode"""

    bl_idname = "bim.create_drawing"
    bl_label = "Create Drawing"
    bl_description = (
        "Creates/refreshes a .svg drawing based on currently active camera.\n\n"
        + "SHIFT+CLICK to print all selected drawings"
    )
    print_all: bpy.props.BoolProperty(name="Print All", default=False, options={"SKIP_SAVE"})
    sync: bpy.props.BoolProperty(
        name="Sync Before Creating Drawing",
        description="Could save some time if you're sure IFC and current Blender session are already in sync",
        default=True,
    )

    @classmethod
    def poll(cls, context):
        return bool(tool.Ifc.get() and tool.Drawing.is_drawing_active())

    def invoke(self, context, event):
        # printing all drawings on shift+click
        # make sure to use SKIP_SAVE on property, otherwise it might get stuck
        if event.type == "LEFTMOUSE" and event.shift:
            self.print_all = True
        return self.execute(context)

    def execute(self, context):
        self.props = context.scene.DocProperties

        if self.print_all:
            original_drawing_id = self.props.active_drawing_id
            drawings_to_print = [d.ifc_definition_id for d in self.props.drawings if d.is_selected and d.is_drawing]
        else:
            drawings_to_print = [self.props.active_drawing_id]

        for drawing_i, drawing_id in enumerate(drawings_to_print):
            self.drawing_index = drawing_i
            if self.print_all:
                bpy.ops.bim.activate_drawing(drawing=drawing_id, camera_view_point=False)

            self.camera = context.scene.camera
            self.camera_element = tool.Ifc.get_entity(self.camera)
            self.camera_document = tool.Drawing.get_drawing_document(self.camera_element)
            self.file = IfcStore.get_file()

            with profile("Drawing generation process"):
                with profile("Initialize drawing generation process"):
                    self.cprops = self.camera.data.BIMCameraProperties
                    self.drawing = self.file.by_id(drawing_id)
                    self.drawing_name = self.drawing.Name
                    self.metadata = tool.Drawing.get_drawing_metadata(self.camera_element)
                    self.get_scale(context)
                    if self.cprops.update_representation(self.camera):
                        bpy.ops.bim.update_representation(obj=self.camera.name, ifc_representation_class="")

                    self.svg_writer = svgwriter.SvgWriter()
                    self.svg_writer.human_scale = self.human_scale
                    self.svg_writer.scale = self.scale
                    self.svg_writer.data_dir = context.scene.BIMProperties.data_dir
                    self.svg_writer.camera = self.camera
                    self.svg_writer.camera_width, self.svg_writer.camera_height = self.get_camera_dimensions()
                    self.svg_writer.camera_projection = tuple(
                        self.camera.matrix_world.to_quaternion() @ Vector((0, 0, -1))
                    )

                    self.svg_writer.setup_drawing_resource_paths(self.camera_element)

                underlay_svg = None
                linework_svg = None
                annotation_svg = None

                with profile("Generate underlay"):
                    underlay_svg = self.generate_underlay(context)

                with profile("Generate linework"):
                    if tool.Drawing.is_camera_orthographic():
                        linework_svg = self.generate_linework(context)

                with profile("Generate annotation"):
                    if tool.Drawing.is_camera_orthographic():
                        annotation_svg = self.generate_annotation(context)

                with profile("Combine SVG layers"):
                    svg_path = self.combine_svgs(context, underlay_svg, linework_svg, annotation_svg)

            tool.Drawing.open_with_user_command(
                context.preferences.addons["blenderbim"].preferences.svg_command, svg_path
            )

        if self.print_all:
            bpy.ops.bim.activate_drawing(drawing=original_drawing_id, camera_view_point=False)
        return {"FINISHED"}

    def get_camera_dimensions(self):
        render = bpy.context.scene.render
        if self.is_landscape(render):
            width = self.camera.data.ortho_scale
            height = width / render.resolution_x * render.resolution_y
        else:
            height = self.camera.data.ortho_scale
            width = height / render.resolution_y * render.resolution_x
        return width, height

    def combine_svgs(self, context, underlay, linework, annotation):
        # Hacky :)
        svg_path = self.get_svg_path()
        with open(svg_path, "w") as outfile:
            self.svg_writer.create_blank_svg(svg_path).define_boilerplate()
            boilerplate = self.svg_writer.svg.tostring()
            outfile.write(boilerplate.replace("</svg>", ""))
            if underlay:
                with open(underlay) as infile:
                    for i, line in enumerate(infile):
                        if i < 2:
                            continue
                        elif "</svg>" in line:
                            continue
                        outfile.write(line)
                shutil.copyfile(os.path.splitext(underlay)[0] + ".png", os.path.splitext(svg_path)[0] + "-underlay.png")
            if linework:
                with open(linework) as infile:
                    should_skip = False
                    for i, line in enumerate(infile):
                        if i == 0:
                            continue
                        if "</svg>" in line:
                            continue
                        elif "<defs>" in line:
                            should_skip = True
                            continue
                        elif "</defs>" in line:
                            should_skip = False
                            continue
                        elif should_skip:
                            continue
                        outfile.write(line)
            if annotation:
                with open(annotation) as infile:
                    for i, line in enumerate(infile):
                        if i < 2:
                            continue
                        if "</svg>" in line:
                            continue
                        outfile.write(line)
            outfile.write("</svg>")
        return svg_path

    def generate_underlay(self, context):
        if not ifcopenshell.util.element.get_pset(self.drawing, "EPset_Drawing", "HasUnderlay"):
            return
        svg_path = self.get_svg_path(cache_type="underlay")
        if os.path.isfile(svg_path) and self.props.should_use_underlay_cache:
            return svg_path

        visible_object_names = {obj.name for obj in bpy.context.visible_objects}
        for obj in bpy.context.view_layer.objects:
            obj.hide_render = obj.name not in visible_object_names

        context.scene.render.filepath = svg_path[0:-4] + ".png"
        drawing_style = context.scene.DocProperties.drawing_styles[self.cprops.active_drawing_style_index]

        if drawing_style.render_type == "DEFAULT":
            bpy.ops.render.render(write_still=True)
        else:
            previous_visibility = {}
            for obj in self.camera.BIMObjectProperties.collection.objects:
                if bpy.context.view_layer.objects.get(obj.name):
                    previous_visibility[obj.name] = obj.hide_get()
                    obj.hide_set(True)
            for obj in context.visible_objects:
                if (
                    (not obj.data and not obj.instance_collection)
                    or isinstance(obj.data, bpy.types.Camera)
                    or "IfcGrid/" in obj.name
                    or "IfcGridAxis/" in obj.name
                    or "IfcOpeningElement/" in obj.name
                ):
                    if bpy.context.view_layer.objects.get(obj.name):
                        previous_visibility[obj.name] = obj.hide_get()
                        obj.hide_set(True)

            space = self.get_view_3d(context.screen.areas)
            previous_shading = space.shading.type
            previous_format = context.scene.render.image_settings.file_format
            space.shading.type = "RENDERED"
            context.scene.render.image_settings.file_format = "PNG"
            bpy.ops.render.opengl(write_still=True)
            space.shading.type = previous_shading
            context.scene.render.image_settings.file_format = previous_format

            for name, value in previous_visibility.items():
                bpy.data.objects[name].hide_set(value)

        self.svg_writer.create_blank_svg(svg_path).draw_underlay(context.scene.render.filepath).save()
        return svg_path

    def get_linework_contexts(self, ifc, target_view) -> LineworkContexts:
        plan_body_target_contexts = []
        plan_body_model_contexts = []
        model_body_target_contexts = []
        model_body_model_contexts = []

        plan_annotation_target_contexts = []
        plan_annotation_model_contexts = []
        model_annotation_target_contexts = []
        model_annotation_model_contexts = []

        for rep_context in ifc.by_type("IfcGeometricRepresentationContext"):
            if rep_context.is_a("IfcGeometricRepresentationSubContext"):
                if rep_context.ContextType == "Plan":
                    if rep_context.ContextIdentifier in ["Body", "Facetation"]:
                        if rep_context.TargetView == target_view:
                            plan_body_target_contexts.append(rep_context.id())
                        elif rep_context.TargetView == "MODEL_VIEW":
                            plan_body_model_contexts.append(rep_context.id())
                    elif rep_context.ContextIdentifier == "Annotation":
                        if rep_context.TargetView == target_view:
                            plan_annotation_target_contexts.append(rep_context.id())
                        elif rep_context.TargetView == "MODEL_VIEW":
                            plan_annotation_model_contexts.append(rep_context.id())
                elif rep_context.ContextType == "Model":
                    if rep_context.ContextIdentifier in ["Body", "Facetation"]:
                        if rep_context.TargetView == target_view:
                            model_body_target_contexts.append(rep_context.id())
                        elif rep_context.TargetView == "MODEL_VIEW":
                            model_body_model_contexts.append(rep_context.id())
                    elif rep_context.ContextIdentifier == "Annotation":
                        if rep_context.TargetView == target_view:
                            model_annotation_target_contexts.append(rep_context.id())
                        elif rep_context.TargetView == "MODEL_VIEW":
                            model_annotation_model_contexts.append(rep_context.id())
            elif rep_context.ContextType == "Model":
                # You should never purely assign to a "Model" context, but
                # if you do, this is what we assume your intention is.
                model_body_model_contexts.append(rep_context.id())
                continue

        body_contexts = (
            [
                plan_body_target_contexts,
                plan_body_model_contexts,
                model_body_target_contexts,
                model_body_model_contexts,
            ]
            if target_view in ["PLAN_VIEW", "REFLECTED_PLAN_VIEW"]
            else [
                model_body_target_contexts,
                model_body_model_contexts,
            ]
        )

        annotation_contexts = (
            [
                plan_annotation_target_contexts,
                plan_annotation_model_contexts,
                model_annotation_target_contexts,
                model_annotation_model_contexts,
            ]
            if target_view in ["PLAN_VIEW", "REFLECTED_PLAN_VIEW"]
            else [
                model_annotation_target_contexts,
                model_annotation_model_contexts,
            ]
        )

        return LineworkContexts(body_contexts, annotation_contexts)

    def serialize_contexts_elements(
        self,
        ifc: ifcopenshell.file,
        tree: ifcopenshell.geom.tree,
        contexts: LineworkContexts,
        context_type: str,
        drawing_elements: set[ifcopenshell.entity_instance],
        target_view: str,
    ) -> None:
        drawing_elements = drawing_elements.copy()
        contexts = getattr(contexts, context_type)
        for context in contexts:
            with profile(f"Processing {context_type} context"):
                if not context or not drawing_elements:
                    continue
                geom_settings = ifcopenshell.geom.settings(
                    DISABLE_TRIANGULATION=True, STRICT_TOLERANCE=True, INCLUDE_CURVES=True
                )
                if ifc.by_id(context[0]).ContextType == "Plan" and "PLAN_VIEW" in target_view:
                    offset = ifcopenshell.ifcopenshell_wrapper.float_array_3()
                    # A 2mm Z offset to combat Z-fighting in plan or RCPs
                    offset[2] = 0.002 if target_view == "PLAN_VIEW" else -0.002
                    geom_settings.offset = offset
                geom_settings.set_context_ids(context)
                it = ifcopenshell.geom.iterator(
                    geom_settings, ifc, multiprocessing.cpu_count(), include=drawing_elements
                )
                processed = set()
                for elem in it:
                    processed.add(ifc.by_id(elem.id))
                    self.serialiser.write(elem)
                    tree.add_element(elem)
                drawing_elements -= processed

    def generate_linework(self, context: bpy.types.Context) -> Union[str, None]:
        if not ifcopenshell.util.element.get_pset(self.drawing, "EPset_Drawing", "HasLinework"):
            return
        svg_path = self.get_svg_path(cache_type="linework")
        if os.path.isfile(svg_path) and self.props.should_use_linework_cache:
            return svg_path

        # in case of printing multiple drawings we need to sync just once
        if self.sync and self.drawing_index == 0:
            with profile("sync"):
                # All very hackish whilst prototyping
                exporter = blenderbim.bim.export_ifc.IfcExporter(None)
                exporter.file = tool.Ifc.get()
                invalidated_elements = exporter.sync_all_objects(skip_unlinking=True)
                invalidated_elements += exporter.sync_edited_objects()
                invalidated_guids = [e.GlobalId for e in invalidated_elements if hasattr(e, "GlobalId")]
                cache = IfcStore.get_cache()
                [cache.remove(guid) for guid in invalidated_guids]

        # If we have already calculated it in the SVG in the past, don't recalculate
        edited_guids = set()
        for obj in IfcStore.edited_objs:
            element = tool.Ifc.get_entity(obj)
            edited_guids.add(element.GlobalId) if hasattr(element, "GlobalId") else None
        cached_linework = set()
        if os.path.isfile(svg_path):
            tree = etree.parse(svg_path)
            root = tree.getroot()
            cached_linework = {
                el.get("{http://www.ifcopenshell.org/ns}guid")
                for el in root.findall(".//{http://www.w3.org/2000/svg}g[@{http://www.ifcopenshell.org/ns}guid]")
            }
        cached_linework -= edited_guids

        files = {context.scene.BIMProperties.ifc_file: tool.Ifc.get()}

        for ifc_path, ifc in files.items():
            # Don't use draw.main() just whilst we're prototyping and experimenting
            # TODO: hash paths are never used
            ifc_hash = hashlib.md5(ifc_path.encode("utf-8")).hexdigest()
            ifc_cache_path = os.path.join(context.scene.BIMProperties.data_dir, "cache", f"{ifc_hash}.h5")

            # Get all representation contexts to see what we're dealing with.
            # Drawings only draw bodies and annotations (and facetation, due to a Revit bug).
            # A drawing prioritises a target view context first, followed by a model view context as a fallback.
            # Specifically for PLAN_VIEW and REFLECTED_PLAN_VIEW, any Plan context is also prioritised.
            target_view = ifcopenshell.util.element.get_psets(self.camera_element)["EPset_Drawing"]["TargetView"]
            contexts = self.get_linework_contexts(ifc, target_view)
            drawing_elements = tool.Drawing.get_drawing_elements(self.camera_element)

            self.setup_serialiser(ifc, target_view)
            tree = ifcopenshell.geom.tree()
            tree.enable_face_styles(True)

            self.serialize_contexts_elements(ifc, tree, contexts, "body", drawing_elements, target_view)
            self.serialize_contexts_elements(ifc, tree, contexts, "annotation", drawing_elements, target_view)

            if self.camera_element not in drawing_elements:
                with profile("Camera element"):
                    # The camera must always be included, regardless of any include/exclude filters.
                    geom_settings = ifcopenshell.geom.settings(DISABLE_TRIANGULATION=True, STRICT_TOLERANCE=True)
                    it = ifcopenshell.geom.iterator(geom_settings, ifc, include=[self.camera_element])
                    for elem in it:
                        self.serialiser.write(elem)

        with profile("Finalizing"):
            self.serialiser.finalize()
        results = self.svg_buffer.get_value()

        root = etree.fromstring(results)

        group = root.find("{http://www.w3.org/2000/svg}g")
        if group is None:
            with open(svg_path, "wb") as svg:
                svg.write(etree.tostring(root))

            return svg_path

        self.move_projection_to_bottom(root)
        self.merge_linework_and_add_metadata(root)

        if self.camera.data.BIMCameraProperties.calculate_shapely_surfaces:
            # shapely variant
            group = root.find("{http://www.w3.org/2000/svg}g")
            nm = group.attrib["{http://www.ifcopenshell.org/ns}name"]
            m4 = np.array(json.loads(group.attrib["{http://www.ifcopenshell.org/ns}plane"]))
            m3 = np.array(json.loads(group.attrib["{http://www.ifcopenshell.org/ns}matrix3"]))
            m44 = np.eye(4)
            m44[0][0:2] = m3[0][0:2]
            m44[1][0:2] = m3[1][0:2]
            m44[0][3] = m3[0][2]
            m44[1][3] = m3[1][2]
            m44 = np.linalg.inv(m44)

            elements_with_faces = set()
            for element in drawing_elements.copy():
                obj = tool.Ifc.get_object(element)
                if obj and obj.type == "MESH" and len(obj.data.polygons):
                    elements_with_faces.add(element.GlobalId)

            projections = root.xpath(
                ".//svg:g[contains(@class, 'projection')]", namespaces={"svg": "http://www.w3.org/2000/svg"}
            )

            boundary_lines = []
            for projection in projections:
                global_id = projection.attrib["{http://www.ifcopenshell.org/ns}guid"]
                if global_id not in elements_with_faces:
                    continue
                for path in projection.findall("./{http://www.w3.org/2000/svg}path"):
                    start, end = [[float(o) for o in co[1:].split(",")] for co in path.attrib["d"].split()]
                    if start == end:
                        continue
                    # Extension by 0.5mm is necessary to ensure lines overlap with other diagonal lines
                    start, end = tool.Drawing.extend_line(start, end, 0.5)
                    boundary_lines.append(shapely.LineString([start, end]))

            unioned_boundaries = shapely.union_all(shapely.GeometryCollection(boundary_lines))
            closed_polygons = shapely.polygonize(unioned_boundaries.geoms)

            for polygon in closed_polygons.geoms:
                # Less than 1mm2 is not worth styling on sheet
                if polygon.area < 1:
                    continue
                centroid = polygon.centroid
                internal_point = centroid if polygon.contains(centroid) else polygon.representative_point()
                if internal_point:
                    internal_point = [internal_point.x, internal_point.y]
                    a, b = self.drawing_to_model_co(m44, m4, internal_point, 0.0), self.drawing_to_model_co(
                        m44, m4, internal_point, -100.0
                    )
                    inside_elements = [e for e in tree.select(self.pythonize(a)) if not e.is_a("IfcAnnotation")]
                    if not inside_elements:
                        elements = [
                            e
                            for e in tree.select_ray(self.pythonize(a), self.pythonize(b - a))
                            if not e.instance.is_a("IfcAnnotation")
                            and tool.Cad.is_point_on_edge(
                                Vector(list(e.position)), (Vector(self.pythonize(a)), Vector(self.pythonize(b)))
                            )
                        ]
                        if elements:
                            path = etree.Element("path")
                            d = (
                                "M"
                                + " L".join([",".join([str(o) for o in co]) for co in polygon.exterior.coords[0:-1]])
                                + " Z"
                            )
                            for interior in polygon.interiors:
                                d += (
                                    " M"
                                    + " L".join([",".join([str(o) for o in co]) for co in interior.coords[0:-1]])
                                    + " Z"
                                )
                            path.attrib["d"] = d
                            classes = self.get_svg_classes(ifc.by_id(elements[0].instance.id()))
                            classes.append(f"intpoint-{internal_point}")
                            classes.append(f"ab-{a}, {b}")
                            for i, ray_result in enumerate(elements):
                                classes.append(f"el{i}-{ray_result.instance.id()}")
                                classes.append(f"el{i}-pos-{list(ray_result.position)}")
                                classes.append(f"el{i}-dst-{ray_result.distance}")
                            classes.append("surface")
                            path.set("class", " ".join(list(classes)))
                            group.insert(0, path)

        if self.camera.data.BIMCameraProperties.calculate_svgfill_surfaces:
            results = etree.tostring(root).decode("utf8")
            svg_data_1 = results
            from xml.dom.minidom import parseString

            def yield_groups(n):
                if n.nodeType == n.ELEMENT_NODE and n.tagName == "g":
                    yield n
                for c in n.childNodes:
                    yield from yield_groups(c)

            dom1 = parseString(svg_data_1)
            svg1 = dom1.childNodes[0]
            groups1 = [g for g in yield_groups(svg1) if "projection" in g.getAttribute("class")]

            ls_groups = ifcopenshell.ifcopenshell_wrapper.svg_to_line_segments(results, "projection")

            for i, (ls, g1) in enumerate(zip(ls_groups, groups1)):
                projection, g1 = g1, g1.parentNode

                svgfill_context = ifcopenshell.ifcopenshell_wrapper.context(
                    ifcopenshell.ifcopenshell_wrapper.EXACT_CONSTRUCTIONS, 1.0e-3
                )

                # EXACT_CONSTRUCTIONS is significantly faster than FILTERED_CARTESIAN_QUOTIENT
                # remove duplicates (without tolerance)
                ls = [l for l in map(tuple, set(map(frozenset, ls))) if len(l) == 2 and l[0] != l[1]]
                svgfill_context.add(ls)

                num_passes = 0

                for iteration in range(num_passes + 1):
                    # initialize empty group, note that in the current approach only one
                    # group is stored
                    ps = ifcopenshell.ifcopenshell_wrapper.svg_groups_of_polygons()
                    if iteration != 0 or svgfill_context.build():
                        svgfill_context.write(ps)

                    if iteration != num_passes:
                        pairs = svgfill_context.get_face_pairs()
                        semantics = [None] * (max(pairs) + 1)

                    # Reserialize cells into an SVG string
                    svg_data_2 = ifcopenshell.ifcopenshell_wrapper.polygons_to_svg(ps, True)

                    # We parse both SVG files to create on document with the combination of sections from
                    # the output directly from the serializer and the cells found from the hidden line
                    # rendering
                    dom2 = parseString(svg_data_2)
                    svg2 = dom2.childNodes[0]
                    # file 2 only has the groups we are interested in.
                    # in fact in the approach, it's only a single group

                    g2 = list(yield_groups(svg2))[0]

                    # These are attributes on the original group that we can use to reconstruct
                    # a 4x4 matrix of the projection used in the SVG generation process
                    nm = g1.getAttribute("ifc:name")
                    m4 = np.array(json.loads(g1.getAttribute("ifc:plane")))
                    m3 = np.array(json.loads(g1.getAttribute("ifc:matrix3")))
                    m44 = np.eye(4)
                    m44[0][0:2] = m3[0][0:2]
                    m44[1][0:2] = m3[1][0:2]
                    m44[0][3] = m3[0][2]
                    m44[1][3] = m3[1][2]
                    m44 = np.linalg.inv(m44)

                    # Loop over the cell paths
                    for pi, p in enumerate(g2.getElementsByTagName("path")):
                        d = p.getAttribute("d")
                        coords = [co[1:].split(",") for co in d.split() if co[1:]]
                        polygon = shapely.Polygon(coords)
                        # 1mm2 polygons aren't worth styling in paperspace. Raycasting is expensive!
                        if polygon.area < 1:
                            continue
                        # point inside is an attribute that comes from line_segments_to_polygons() polygons_to_svg?
                        # it is an arbitrary point guaranteed to be inside the polygon and outside
                        # of any potential inner bounds. We can use this to construct a ray to find
                        # the face of the IFC element that the cell belongs to.
                        assert p.hasAttribute("ifc:pointInside")

                        xy = list(map(float, p.getAttribute("ifc:pointInside").split(",")))

                        a, b = self.drawing_to_model_co(m44, m4, xy, 0.0), self.drawing_to_model_co(m44, m4, xy, -100.0)

                        inside_elements = [e for e in tree.select(self.pythonize(a)) if not e.is_a("IfcAnnotation")]
                        if inside_elements:
                            elements = None
                            if iteration != num_passes:
                                semantics[pi] = (inside_elements[0], -1)
                        else:
                            elements = [
                                e
                                for e in tree.select_ray(self.pythonize(a), self.pythonize(b - a))
                                if not e.instance.is_a("IfcAnnotation")
                            ]

                        if elements:
                            classes = self.get_svg_classes(ifc.by_id(elements[0].instance.id()))
                            classes.append("projection")

                            if iteration != num_passes:
                                semantics[pi] = elements[0]
                        else:
                            classes = ["projection"]

                        p.setAttribute("style", "")
                        p.setAttribute("class", " ".join(classes))

                    if iteration != num_passes:
                        to_remove = []

                        for he_idx in range(0, len(pairs), 2):
                            # @todo instead of ray_distance, better do (x.point - y.point).dot(x.normal)
                            # to see if they're coplanar, because ray-distance will be different in case
                            # of element surfaces non-orthogonal to the view direction

                            def format(x):
                                if x is None:
                                    return None
                                elif isinstance(x, tuple):
                                    # found to be inside element using tree.select() no face or style info
                                    return x
                                else:
                                    return (x.instance.is_a(), x.ray_distance, tuple(x.position))

                            pp = pairs[he_idx : he_idx + 2]
                            if pp == (-1, -1):
                                continue
                            data = list(map(format, map(semantics.__getitem__, pp)))
                            if None not in data and data[0][0] == data[1][0] and abs(data[0][1] - data[1][1]) < 1.0e-5:
                                to_remove.append(he_idx // 2)
                                # Print edge index and semantic data
                                # print(he_idx // 2, *data)

                        svgfill_context.merge(to_remove)

                # Swap the XML nodes from the files
                # Remove the original hidden line node we still have in the serializer output
                g1.removeChild(projection)
                g2.setAttribute("class", "projection")
                # Find the children of the projection node parent
                children = [x for x in g1.childNodes if x.nodeType == x.ELEMENT_NODE]
                if children:
                    # Insert the new semantically enriched cell-based projection node
                    # *before* the node with sections from the serializer. SVG derives
                    # draw order from node order in the DOM so sections are draw over
                    # the projections.
                    g1.insertBefore(g2, children[0])
                else:
                    # This generally shouldn't happen
                    g1.appendChild(g2)

            results = dom1.toxml()
            results = results.encode("ascii", "xmlcharrefreplace")
            root = etree.fromstring(results)

        # Spaces are handled as a special case, since they are often overlayed
        # in addition to elements. For example, a space should not obscure
        # other elements in projection. Spaces should also not override cut
        # elements but instead be drawn in addition to cut elements.
        spaces = tool.Drawing.get_drawing_spaces(self.camera_element)

        group = root.findall(".//{http://www.w3.org/2000/svg}g")[0]

        self.svg_writer.calculate_scale()
        x_offset = self.svg_writer.raw_width / 2
        y_offset = self.svg_writer.raw_height / 2

        for space in spaces:
            obj = tool.Ifc.get_object(space)
            if not obj or not tool.Drawing.is_intersecting_camera(obj, self.camera):
                continue
            verts, edges = tool.Drawing.bisect_mesh(obj, self.camera)
            verts = [self.svg_writer.project_point_onto_camera(Vector(v)) for v in verts]
            line_strings = [
                shapely.LineString(
                    [
                        (
                            (x_offset + verts[e[0]][0]) * self.svg_writer.svg_scale,
                            (y_offset - verts[e[0]][1]) * self.svg_writer.svg_scale,
                        ),
                        (
                            (x_offset + verts[e[1]][0]) * self.svg_writer.svg_scale,
                            (y_offset - verts[e[1]][1]) * self.svg_writer.svg_scale,
                        ),
                    ]
                )
                for e in edges
            ]
            closed_polygons = shapely.polygonize(line_strings)
            for polygon in closed_polygons.geoms:
                classes = self.get_svg_classes(space)
                path = etree.Element("path")
                d = "M" + " L".join([",".join([str(o) for o in co]) for co in polygon.exterior.coords[0:-1]]) + " Z"
                for interior in polygon.interiors:
                    d += " M" + " L".join([",".join([str(o) for o in co]) for co in interior.coords[0:-1]]) + " Z"
                path.attrib["d"] = d
                path.set("class", " ".join(list(classes)))
                group.append(path)

        with open(svg_path, "wb") as svg:
            svg.write(etree.tostring(root))

        return svg_path

    def setup_serialiser(self, ifc, target_view):
        self.svg_settings = ifcopenshell.geom.settings(
            DISABLE_TRIANGULATION=True, STRICT_TOLERANCE=True, INCLUDE_CURVES=True
        )
        self.svg_buffer = ifcopenshell.geom.serializers.buffer()
        self.serialiser = ifcopenshell.geom.serializers.svg(self.svg_buffer, self.svg_settings)
        self.serialiser.setFile(ifc)
        self.serialiser.setWithoutStoreys(True)
        self.serialiser.setPolygonal(True)
        self.serialiser.setUseHlrPoly(True)
        # Objects with more than these edges are rendered as wireframe instead of HLR for optimisation
        self.serialiser.setProfileThreshold(10000)
        self.serialiser.setUseNamespace(True)
        self.serialiser.setAlwaysProject(True)
        self.serialiser.setAutoElevation(False)
        self.serialiser.setAutoSection(False)
        self.serialiser.setPrintSpaceNames(False)
        self.serialiser.setPrintSpaceAreas(False)
        self.serialiser.setDrawDoorArcs(False)
        self.serialiser.setNoCSS(True)
        self.serialiser.setElevationRefGuid(self.camera_element.GlobalId)
        self.serialiser.setScale(self.scale)
        self.serialiser.setSubtractionSettings(ifcopenshell.ifcopenshell_wrapper.ALWAYS)
        self.serialiser.setUsePrefiltering(True)  # See #3359
        self.serialiser.setUnifyInputs(True)
        self.serialiser.setSegmentProjection(True)
        if target_view == "REFLECTED_PLAN_VIEW":
            self.serialiser.setMirrorY(True)
        # tree = ifcopenshell.geom.tree()
        # This instructs the tree to explode BReps into faces and return
        # the style of the face when running tree.select_ray()
        # tree.enable_face_styles(True)

    def get_svg_classes(self, element):
        classes = [element.is_a()]
        material = ifcopenshell.util.element.get_material(element, should_skip_usage=True)
        material_name = ""
        if material:
            if material.is_a("IfcMaterialLayerSet"):
                material_name = material.LayerSetName or "null"
            else:
                material_name = getattr(material, "Name", "null") or "null"
            material_name = tool.Drawing.canonicalise_class_name(material_name)
            classes.append(f"material-{material_name}")
        else:
            classes.append(f"material-null")

        for key in self.metadata:
            value = ifcopenshell.util.selector.get_element_value(element, key)
            if value:
                classes.append(
                    tool.Drawing.canonicalise_class_name(key) + "-" + tool.Drawing.canonicalise_class_name(str(value))
                )
        return classes

    def is_manifold(self, obj):
        result = self.is_manifold_cache.get(obj.data.name, None)
        if result is not None:
            return result

        bm = bmesh.new()
        bm.from_mesh(obj.data)
        for edge in bm.edges:
            if not edge.is_manifold:
                bm.free()
                self.is_manifold_cache[obj.data.name] = False
                return False
        self.is_manifold_cache[obj.data.name] = True
        return True

    def merge_linework_and_add_metadata(self, root):
        join_criteria = ifcopenshell.util.element.get_pset(self.camera_element, "EPset_Drawing", "JoinCriteria")
        if join_criteria:
            join_criteria = join_criteria.split(",")
        else:
            # Drawing convention states that same objects classes with the same material are merged when cut.
            join_criteria = ["class", "material.Name", "/Pset_.*Common/.Status", "EPset_Status.Status"]

        group = root.find("{http://www.w3.org/2000/svg}g")
        joined_paths = {}
        self.is_manifold_cache = {}

        ifc = tool.Ifc.get()
        for el in root.findall(".//{http://www.w3.org/2000/svg}g[@{http://www.ifcopenshell.org/ns}guid]"):
            element = ifc.by_guid(el.get("{http://www.ifcopenshell.org/ns}guid"))

            if "projection" in el.get("class", "").split():
                classes = self.get_svg_classes(element)
                classes.append("projection")
                el.set("class", " ".join(classes))
                continue
            else:
                classes = self.get_svg_classes(element)
                classes.append("cut")
                el.set("class", " ".join(classes))

            obj = tool.Ifc.get_object(element)
            if not self.is_manifold(obj):
                continue

            # An element group will contain a bunch of paths representing the
            # cut of that element. However IfcOpenShell may not correctly
            # create closed paths. We post-process all paths with shapely to
            # ensure things that should be closed (i.e.
            # shapely.polygonize_full) are, and things which aren't are left
            # alone (e.g. dangles, cuts, invalids). See #3421.
            line_strings = []
            old_paths = []
            has_open_paths = False
            for path in el.findall("{http://www.w3.org/2000/svg}path"):
                for subpath in path.attrib["d"].split("M")[1:]:
                    subpath = "M" + subpath.strip()
                    coords = [[float(o) for o in co[1:].split(",")] for co in subpath.split()]
                    if coords[0] != coords[-1]:
                        has_open_paths = True
                    line_strings.append(shapely.LineString(coords))
                old_paths.append(path)

            results = []
            if has_open_paths:
                unioned_line_strings = shapely.union_all(shapely.GeometryCollection(line_strings))
                if hasattr(unioned_line_strings, "geoms"):
                    results = shapely.polygonize_full(unioned_line_strings.geoms)

            # If we succeeded in generating new path geometry, remove all the
            # old paths and add new ones.
            if results:
                for path in old_paths:
                    path.getparent().remove(path)
            for result in results:
                for geom in result.geoms:
                    path = etree.SubElement(el, "{http://www.w3.org/2000/svg}path")
                    if isinstance(geom, shapely.Polygon):
                        d = (
                            "M"
                            + " L".join([",".join([str(o) for o in co]) for co in geom.exterior.coords[0:-1]])
                            + " Z"
                        )
                        for interior in geom.interiors:
                            d += (
                                " M"
                                + " L".join([",".join([str(o) for o in co]) for co in interior.coords[0:-1]])
                                + " Z"
                            )
                    elif isinstance(geom, shapely.LineString):
                        d = "M" + " L".join([",".join([str(o) for o in co]) for co in geom.coords]) + " Z"
                    path.attrib["d"] = d

            # Architectural convention only merges these objects. E.g. pipe segments and fittings shouldn't merge.
            if not element.is_a("IfcWall") and not element.is_a("IfcSlab"):
                continue

            keys = []
            for query in join_criteria:
                key = ifcopenshell.util.selector.get_element_value(element, query)
                if isinstance(key, (list, tuple)):
                    keys.extend(key)
                else:
                    keys.append(key)

            hash_keys = hash(tuple(keys))

            if el.findall("{http://www.w3.org/2000/svg}path"):
                joined_paths.setdefault(hash_keys, []).append(el)

        for key, els in joined_paths.items():
            polygons = []
            classes = set()

            for el in els:
                classes.update(el.attrib["class"].split())
                classes.add(el.attrib["{http://www.ifcopenshell.org/ns}guid"])
                is_closed_polygon = False
                for path in el.findall("{http://www.w3.org/2000/svg}path"):
                    for subpath in path.attrib["d"].split("M")[1:]:
                        subpath_co = "M" + subpath.strip(" Z")
                        coords = [[float(o) for o in co[1:].split(",")] for co in subpath_co.split()]
                        if subpath.strip().lower().endswith("z"):
                            coords.append(coords[0])
                        if len(coords) > 2 and coords[0] == coords[-1]:
                            is_closed_polygon = True
                            polygons.append(shapely.Polygon(coords))
                if is_closed_polygon:
                    el.getparent().remove(el)

            try:
                merged_polygons = shapely.ops.unary_union(polygons)
            except:
                print("Warning. Portions of the merge failed. Please report a bug!", polygons)
                merged_polygons = polygons

            if type(merged_polygons) == shapely.MultiPolygon:
                merged_polygons = merged_polygons.geoms
            elif type(merged_polygons) == shapely.Polygon:
                merged_polygons = [merged_polygons]
            else:
                merged_polygons = []

            for polygon in merged_polygons:
                g = etree.Element("g")
                path = etree.SubElement(g, "path")
                d = "M" + " L".join([",".join([str(o) for o in co]) for co in polygon.exterior.coords[0:-1]]) + " Z"
                for interior in polygon.interiors:
                    d += " M" + " L".join([",".join([str(o) for o in co]) for co in interior.coords[0:-1]]) + " Z"
                path.attrib["d"] = d
                g.set("class", " ".join(list(classes)))
                group.append(g)

    def drawing_to_model_co(self, m44, m4, xy, z=0.0):
        xyzw = m44 @ np.array(xy + [z, 1.0])
        xyzw[1] *= -1.0
        return (m4 @ xyzw)[0:3]

    def pythonize(self, arr):
        return tuple(map(float, arr))

    def move_projection_to_bottom(self, root):
        # IfcConvert puts the projection afterwards which is not correct since
        # projection should be drawn underneath the cut.
        group = root.find("{http://www.w3.org/2000/svg}g")
        projections = root.xpath(
            ".//svg:g[contains(@class, 'projection')]", namespaces={"svg": "http://www.w3.org/2000/svg"}
        )
        for projection in projections:
            projection.getparent().remove(projection)
            group.insert(0, projection)

    def generate_annotation(self, context):
        if not ifcopenshell.util.element.get_pset(self.drawing, "EPset_Drawing", "HasAnnotation"):
            return
        svg_path = self.get_svg_path(cache_type="annotation")
        if os.path.isfile(svg_path) and self.props.should_use_annotation_cache:
            return svg_path

        elements = tool.Drawing.get_group_elements(tool.Drawing.get_drawing_group(self.camera_element))
        filtered_drawing_elements = tool.Drawing.get_drawing_elements(self.camera_element)
        filtered_drawing_annotations = {e for e in filtered_drawing_elements if e.is_a("IfcAnnotation")}
        elements = {e for e in elements if e in filtered_drawing_elements}
        elements = list(elements | filtered_drawing_annotations)

        annotations = sorted(
            elements, key=lambda a: (tool.Drawing.get_annotation_z_index(a), 1 if a.ObjectType == "TEXT" else 0)
        )

        precision = ifcopenshell.util.element.get_pset(self.camera_element, "EPset_Drawing", "MetricPrecision")
        if not precision:
            precision = ifcopenshell.util.element.get_pset(self.camera_element, "EPset_Drawing", "ImperialPrecision")

        decimal_places = ifcopenshell.util.element.get_pset(self.camera_element, "EPset_Drawing", "DecimalPlaces")
        self.svg_writer.metadata = self.metadata
        self.svg_writer.create_blank_svg(svg_path).draw_annotations(annotations, precision, decimal_places).save()

        return svg_path

    def get_scale(self, context):
        diagram_scale = tool.Drawing.get_diagram_scale(self.camera)
        self.human_scale = diagram_scale["HumanScale"]
        self.scale = tool.Drawing.get_scale_ratio(diagram_scale["Scale"])

        if ifcopenshell.util.element.get_pset(self.camera_element, "EPset_Drawing", "IsNTS"):
            self.human_scale = "NTS"

    def is_landscape(self, render):
        return render.resolution_x > render.resolution_y

    def get_view_3d(self, areas):
        for area in areas:
            if area.type != "VIEW_3D":
                continue
            for space in area.spaces:
                if space.type != "VIEW_3D":
                    continue
                return space

    def get_material_name(self, element: ifcopenshell.entity_instance) -> str:
        if hasattr(element, "Name") and element.Name:
            return element.Name
        elif hasattr(element, "LayerSetName") and element.LayerSetName:
            return element.LayerSetName
        return "mat-" + str(element.id())

    def get_svg_path(self, cache_type: Optional[str] = None) -> str:
        drawing_path = tool.Drawing.get_document_uri(self.camera_document)
        drawings_dir = os.path.dirname(drawing_path)

        if cache_type:
            drawings_dir = os.path.join(drawings_dir, "cache")
            os.makedirs(drawings_dir, exist_ok=True)
            return os.path.join(drawings_dir, f"{self.drawing_name}-{cache_type}.svg")
        os.makedirs(drawings_dir, exist_ok=True)
        return drawing_path


class AddAnnotation(bpy.types.Operator, Operator):
    bl_idname = "bim.add_annotation"
    bl_label = "Add Annotation"
    bl_options = {"REGISTER", "UNDO"}
    object_type: bpy.props.StringProperty()
    data_type: bpy.props.StringProperty()
    description: bpy.props.StringProperty()

    @classmethod
    def poll(cls, context):
        return IfcStore.get_file() and context.scene.camera

    @classmethod
    def description(cls, context, operator):
        return operator.description or ""

    def _execute(self, context):
        drawing = tool.Ifc.get_entity(context.scene.camera)
        if not drawing:
            self.report({"WARNING"}, "Not a BIM camera")
            return

        r = core.add_annotation(tool.Ifc, tool.Collector, tool.Drawing, drawing=drawing, object_type=self.object_type)
        if isinstance(r, str):
            self.report({"WARNING"}, r)


class AddSheet(bpy.types.Operator, Operator):
    bl_idname = "bim.add_sheet"
    bl_label = "Add Sheet"
    bl_options = {"REGISTER", "UNDO"}

    def _execute(self, context):
        core.add_sheet(tool.Ifc, tool.Drawing, titleblock=context.scene.DocProperties.titleblock)


class OpenSheet(bpy.types.Operator, Operator):
    bl_idname = "bim.open_sheet"
    bl_label = "Open Sheet Layout"
    bl_options = {"REGISTER", "UNDO"}

    def _execute(self, context):
        self.props = context.scene.DocProperties
        sheet = tool.Ifc.get().by_id(self.props.sheets[self.props.active_sheet_index].ifc_definition_id)
        sheet_builder = sheeter.SheetBuilder()
        sheet_builder.update_sheet_drawing_sizes(sheet)
        core.open_sheet(tool.Drawing, sheet=sheet)


class AddDrawingToSheet(bpy.types.Operator, Operator):
    bl_idname = "bim.add_drawing_to_sheet"
    bl_label = "Add Drawing To Sheet"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        props = context.scene.DocProperties
        return props.drawings and props.sheets and context.scene.BIMProperties.data_dir

    def _execute(self, context):
        props = context.scene.DocProperties
        active_drawing = props.drawings[props.active_drawing_index]
        active_sheet = tool.Drawing.get_active_sheet(context)
        drawing = tool.Ifc.get().by_id(active_drawing.ifc_definition_id)
        drawing_reference = tool.Drawing.get_drawing_document(drawing)

        sheet = tool.Ifc.get().by_id(active_sheet.ifc_definition_id)
        if not sheet.is_a("IfcDocumentInformation"):
            return

        references = tool.Drawing.get_document_references(sheet)

        has_drawing = False
        for reference in references:
            if reference.Location == drawing_reference.Location:
                has_drawing = True
                break
        if has_drawing:
            return

        if not tool.Drawing.does_file_exist(tool.Drawing.get_document_uri(drawing_reference)):
            self.report({"ERROR"}, "The drawing must be generated before adding to a sheet.")
            return

        reference = tool.Ifc.run("document.add_reference", information=sheet)
        id_attr = "ItemReference" if tool.Ifc.get_schema() == "IFC2X3" else "Identification"
        attributes = {
            id_attr: str(len([r for r in references if r.Description in ("DRAWING", "SCHEDULE")]) + 1),
            "Location": drawing_reference.Location,
            "Description": "DRAWING",
        }
        tool.Ifc.run("document.edit_reference", reference=reference, attributes=attributes)
        sheet_builder = sheeter.SheetBuilder()
        sheet_builder.data_dir = context.scene.BIMProperties.data_dir
        sheet_builder.add_drawing(reference, drawing, sheet)

        tool.Drawing.import_sheets()


class RemoveDrawingFromSheet(bpy.types.Operator, Operator):
    bl_idname = "bim.remove_drawing_from_sheet"
    bl_label = "Remove Drawing From Sheet"
    bl_options = {"REGISTER", "UNDO"}
    reference: bpy.props.IntProperty()

    def _execute(self, context):
        reference = tool.Ifc.get().by_id(self.reference)
        sheet = tool.Drawing.get_reference_document(reference)

        sheet_builder = sheeter.SheetBuilder()
        sheet_builder.data_dir = context.scene.BIMProperties.data_dir
        sheet_builder.remove_drawing(reference, sheet)

        tool.Ifc.run("document.remove_reference", reference=reference)

        tool.Drawing.import_sheets()


class CreateSheets(bpy.types.Operator, Operator):
    bl_idname = "bim.create_sheets"
    bl_label = "Create Sheets"
    bl_description = "Build a sheet from the sheet layout"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return context.scene.DocProperties.sheets and context.scene.BIMProperties.data_dir

    def _execute(self, context):
        scene = context.scene
        props = scene.DocProperties
        active_sheet = props.sheets[props.active_sheet_index]
        sheet = tool.Ifc.get().by_id(active_sheet.ifc_definition_id)

        if not sheet.is_a("IfcDocumentInformation"):
            return

        name = os.path.splitext(os.path.basename(tool.Drawing.get_document_uri(sheet)))[0]
        sheet_builder = sheeter.SheetBuilder()
        sheet_builder.data_dir = scene.BIMProperties.data_dir

        references = sheet_builder.build(sheet)
        raster_references = [tool.Ifc.get_relative_uri(r) for r in references["RASTER"]]

        # These variables will be made available to the evaluated commands
        svg = references["SHEET"]
        pdf = os.path.splitext(svg)[0] + ".pdf"
        replacements = {
            "svg": svg,
            "basename": os.path.basename(svg),
            "path": os.path.dirname(svg),
            "pdf": pdf,
            "eps": os.path.splitext(svg)[0] + ".eps",
            "dxf": os.path.splitext(svg)[0] + ".dxf",
        }

        has_sheet_reference = False
        for reference in tool.Drawing.get_document_references(sheet):
            if reference.Description == "SHEET":
                has_sheet_reference = True
            elif reference.Description == "RASTER":
                if reference.Location in raster_references:
                    raster_references.remove(reference.Location)
                else:
                    tool.Ifc.run("document.remove_reference", reference=reference)

        if not has_sheet_reference:
            reference = tool.Ifc.run("document.add_reference", information=sheet)
            tool.Ifc.run(
                "document.edit_reference",
                reference=reference,
                attributes={"Location": tool.Ifc.get_relative_uri(svg), "Description": "SHEET"},
            )

        for raster_reference in raster_references:
            reference = tool.Ifc.run("document.add_reference", information=sheet)
            tool.Ifc.run(
                "document.edit_reference",
                reference=reference,
                attributes={"Location": tool.Ifc.get_relative_uri(raster_reference), "Description": "RASTER"},
            )

        svg2pdf_command = context.preferences.addons["blenderbim"].preferences.svg2pdf_command
        svg2dxf_command = context.preferences.addons["blenderbim"].preferences.svg2dxf_command

        if svg2pdf_command:
            # With great power comes great responsibility. Example:
            # [["inkscape", "svg", "-o", "pdf"]]
            commands = json.loads(svg2pdf_command)
            for command in commands:
                subprocess.run([replacements.get(c, c) for c in command])

        if svg2dxf_command:
            # With great power comes great responsibility. Example:
            # [["inkscape", "svg", "-o", "eps"], ["pstoedit", "-dt", "-f", "dxf:-polyaslines -mm", "eps", "dxf", "-psarg", "-dNOSAFER"]]
            commands = json.loads(svg2dxf_command)
            for command in commands:
                command[0] = shutil.which(command[0]) or command[0]
                subprocess.run([replacements.get(c, c) for c in command])

        if svg2pdf_command:
            tool.Drawing.open_with_user_command(context.preferences.addons["blenderbim"].preferences.pdf_command, pdf)
        else:
            tool.Drawing.open_with_user_command(context.preferences.addons["blenderbim"].preferences.svg_command, svg)


class SelectAllDrawings(bpy.types.Operator):
    bl_idname = "bim.select_all_drawings"
    bl_label = "Select All Drawings"
    view: bpy.props.StringProperty()
    bl_description = "Select all drawings in the drawing list.\n\n" + "SHIFT+CLICK to deselect all drawings"
    select_all: bpy.props.BoolProperty(name="Open All", default=True, options={"SKIP_SAVE"})

    def invoke(self, context, event):
        # deselect all drawings on shift+click
        # make sure to use SKIP_SAVE on property, otherwise it might get stuck
        if event.type == "LEFTMOUSE" and event.shift:
            self.select_all = False
        return self.execute(context)

    def execute(self, context):
        for drawing in context.scene.DocProperties.drawings:
            if drawing.is_selected != self.select_all:
                drawing.is_selected = self.select_all
        return {"FINISHED"}


class OpenDrawing(bpy.types.Operator):
    bl_idname = "bim.open_drawing"
    bl_label = "Open Drawing"
    view: bpy.props.StringProperty()
    bl_description = (
        "Opens a .svg drawing based on currently active camera with default system viewer\n"
        + 'or using "svg_command" from the BlenderBIM preferences (if provided).\n\n'
        + "SHIFT+CLICK to open all selected drawings"
    )
    open_all: bpy.props.BoolProperty(name="Open All", default=False, options={"SKIP_SAVE"})

    def invoke(self, context, event):
        # opening all drawings on shift+click
        # make sure to use SKIP_SAVE on property, otherwise it might get stuck
        if event.type == "LEFTMOUSE" and event.shift:
            self.open_all = True
        return self.execute(context)

    def execute(self, context):
        if self.open_all:
            drawings = [
                tool.Ifc.get().by_id(d.ifc_definition_id) for d in context.scene.DocProperties.drawings if d.is_selected
            ]
        else:
            drawings = [tool.Ifc.get().by_id(context.scene.DocProperties.drawings.get(self.view).ifc_definition_id)]

        drawing_uris = []
        drawings_not_found = []

        for drawing in drawings:
            drawing_uri = tool.Drawing.get_document_uri(tool.Drawing.get_drawing_document(drawing))
            drawing_uris.append(drawing_uri)
            if not os.path.exists(drawing_uri):
                drawings_not_found.append(drawing.Name)

        if drawings_not_found:
            msg = "Some drawings .svg files were not found, need to print them first: \n{}.".format(
                "\n".join(drawings_not_found)
            )
            self.report({"ERROR"}, msg)
            return {"CANCELLED"}

        for drawing_uri in drawing_uris:
            tool.Drawing.open_with_user_command(
                context.preferences.addons["blenderbim"].preferences.svg_command, drawing_uri
            )
        return {"FINISHED"}


class ActivateModel(bpy.types.Operator):
    bl_idname = "bim.activate_model"
    bl_label = "Activate Model"
    bl_options = {"REGISTER", "UNDO"}
    bl_description = "Activates the model view"

    def execute(self, context):
        dprops = bpy.context.scene.DocProperties
        dprops.active_drawing_id = 0

        CutDecorator.uninstall()

        if not bpy.app.background:
            with context.temp_override(**tool.Blender.get_viewport_context()):
                bpy.ops.object.hide_view_clear()
                bpy.ops.bim.activate_status_filters()

        subcontext = ifcopenshell.util.representation.get_context(tool.Ifc.get(), "Model", "Body", "MODEL_VIEW")

        for obj in context.visible_objects:
            element = tool.Ifc.get_entity(obj)
            if not element:
                continue
            model = ifcopenshell.util.representation.get_representation(element, "Model", "Body", "MODEL_VIEW")
            if model:
                current_representation = tool.Geometry.get_active_representation(obj)
                if current_representation != model:
                    blenderbim.core.geometry.switch_representation(
                        tool.Ifc,
                        tool.Geometry,
                        obj=obj,
                        representation=model,
                        should_reload=False,
                        is_global=True,
                        should_sync_changes_first=True,
                    )
        tool.Blender.update_viewport()
        return {"FINISHED"}


class ActivateDrawing(bpy.types.Operator):
    bl_idname = "bim.activate_drawing"
    bl_label = "Activate Drawing"
    bl_options = {"REGISTER", "UNDO"}
    bl_description = "Activates the selected drawing view.\n\n" + "ALT+CLICK to keep the viewport position"

    drawing: bpy.props.IntProperty()
    camera_view_point: bpy.props.BoolProperty(name="Camera View Point", default=True, options={"SKIP_SAVE"})

    def invoke(self, context, event):
        # keep the viewport position on alt+click
        # make sure to use SKIP_SAVE on property, otherwise it might get stuck
        if event.type == "LEFTMOUSE" and event.alt:
            self.camera_view_point = False
        return self.execute(context)

    def execute(self, context):
        drawing = tool.Ifc.get().by_id(self.drawing)
        dprops = bpy.context.scene.DocProperties

        if not self.camera_view_point:
            viewport_position = tool.Blender.get_viewport_position()

        core.activate_drawing_view(tool.Ifc, tool.Drawing, drawing=drawing)

        if not self.camera_view_point:
            tool.Blender.set_viewport_position(viewport_position)

        dprops.active_drawing_id = self.drawing
        # reset DrawingsData to reload_drawing_styles work correctly
        DrawingsData.is_loaded = False
        dprops.drawing_styles.clear()
        if ifcopenshell.util.element.get_pset(drawing, "EPset_Drawing", "HasUnderlay"):
            bpy.ops.bim.reload_drawing_styles()
            bpy.ops.bim.activate_drawing_style()
        core.sync_references(tool.Ifc, tool.Collector, tool.Drawing, drawing=tool.Ifc.get().by_id(self.drawing))
        CutDecorator.install(context)
        tool.Drawing.show_decorations()
        return {"FINISHED"}


class SelectDocIfcFile(bpy.types.Operator):
    bl_idname = "bim.select_doc_ifc_file"
    bl_label = "Select Documentation IFC File"
    bl_options = {"REGISTER", "UNDO"}
    filter_glob: bpy.props.StringProperty(default="*.ifc;*.ifczip;*.ifcxml", options={"HIDDEN"})
    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    index: bpy.props.IntProperty()

    def execute(self, context):
        context.scene.DocProperties.ifc_files[self.index].name = self.filepath
        return {"FINISHED"}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


class ResizeText(bpy.types.Operator):
    bl_idname = "bim.resize_text"
    bl_label = "Resize Text"
    bl_options = {"REGISTER", "UNDO"}
    # TODO: check undo redo

    def execute(self, context):
        for obj in context.scene.camera.BIMObjectProperties.collection.objects:
            if isinstance(obj.data, bpy.types.TextCurve):
                annotation.Annotator.resize_text(obj)
        return {"FINISHED"}


class RemoveDrawing(bpy.types.Operator, Operator):
    bl_idname = "bim.remove_drawing"
    bl_label = "Remove Drawing"
    bl_options = {"REGISTER", "UNDO"}
    bl_description = "Remove currently selected drawing.\n\n" + "SHIFT+CLICK to remove all selected drawings"

    drawing: bpy.props.IntProperty()
    remove_all: bpy.props.BoolProperty(name="Remove All", default=False, options={"SKIP_SAVE"})

    def invoke(self, context, event):
        # removing all selected drawings on shift+click
        # make sure to use SKIP_SAVE on property, otherwise it might get stuck
        if event.type == "LEFTMOUSE" and event.shift:
            self.remove_all = True
        return self.execute(context)

    def _execute(self, context):
        if self.remove_all:
            drawings = [
                tool.Ifc.get().by_id(d.ifc_definition_id) for d in context.scene.DocProperties.drawings if d.is_selected
            ]
        else:
            if not self.drawing:
                self.report({"ERROR"}, "No drawing selected")
                return {"CANCELLED"}
            drawings = [tool.Ifc.get().by_id(self.drawing)]

        removed_drawings = [drawing.id() for drawing in drawings]

        for drawing in drawings:
            sheet_references = tool.Drawing.get_sheet_references(drawing)
            for reference in sheet_references:
                bpy.ops.bim.remove_drawing_from_sheet(reference=reference.id())
            core.remove_drawing(tool.Ifc, tool.Drawing, drawing=drawing)


class ReloadDrawingStyles(bpy.types.Operator):
    bl_idname = "bim.reload_drawing_styles"
    bl_label = "Reload Drawing Styles"
    bl_options = {"REGISTER", "UNDO"}
    bl_description = "Reload drawing styles for the active camera"

    def execute(self, context):
        if not DrawingsData.is_loaded:
            DrawingsData.load()
        drawing_pset_data = DrawingsData.data["active_drawing_pset_data"]
        camera_props = context.active_object.data.BIMCameraProperties

        # added this part as a temporary fallback
        # TODO: should remove it a bit later when projects get more accommodated
        # with saving shadingstyles to ifc and separate json file
        if "ShadingStyles" not in drawing_pset_data:
            ifc_file = tool.Ifc.get()
            pset = ifc_file.by_id(drawing_pset_data["id"])
            edit_properties = {
                "ShadingStyles": (
                    shading_styles_path := tool.Drawing.get_default_drawing_resource_path("ShadingStyles")
                ),
                "CurrentShadingStyle": tool.Drawing.get_default_shading_style(),
            }
            ifcopenshell.api.run("pset.edit_pset", ifc_file, pset=pset, properties=edit_properties)
            tool.Drawing.setup_shading_styles_path(shading_styles_path)

            DrawingsData.load()
            drawing_pset_data = DrawingsData.data["active_drawing_pset_data"]

        if "ShadingStyles" not in drawing_pset_data:
            self.report({"ERROR"}, "Could not find shading styles path in EPset_Drawing.ShadingStyles.")
            return {"CANCELLED"}

        rel_path = drawing_pset_data["ShadingStyles"]
        current_style = drawing_pset_data.get("CurrentShadingStyle", None)

        json_path = Path(tool.Ifc.resolve_uri(rel_path))
        if not json_path.exists():
            ootb_resource = Path(context.scene.BIMProperties.data_dir) / "assets" / "shading_styles.json"
            print(
                f"WARNING. Couldn't find shading_styles for the drawing by the path: {json_path}. "
                f"Default BBIM resource will be copied from {ootb_resource}"
            )
            if ootb_resource.exists():
                os.makedirs(json_path.parent, exist_ok=True)
                shutil.copy(ootb_resource, json_path)

        with open(json_path, "r") as fi:
            shading_styles_json = json.load(fi)

        drawing_styles = context.scene.DocProperties.drawing_styles
        drawing_styles.clear()
        styles = [style for style in shading_styles_json]
        for style_name in styles:
            style_data = shading_styles_json[style_name]
            drawing_style = drawing_styles.add()
            drawing_style["name"] = style_name  # setting as attribute to avoid triggering setter
            drawing_style.render_type = style_data["render_type"]
            drawing_style.raster_style = json.dumps(style_data["raster_style"])

        if current_style is not None:
            camera_props.active_drawing_style_index = styles.index(current_style)

        return {"FINISHED"}


class AddDrawingStyle(bpy.types.Operator, Operator):
    bl_idname = "bim.add_drawing_style"
    bl_label = "Add Drawing Style"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        drawing_styles = context.scene.DocProperties.drawing_styles
        new = drawing_styles.add()
        # drawing style is saved to ifc on rename
        new.name = tool.Blender.ensure_unique_name("New Drawing Style", drawing_styles)
        context.scene.camera.data.BIMCameraProperties.active_drawing_style_index = len(drawing_styles) - 1
        return {"FINISHED"}


class RemoveDrawingStyle(bpy.types.Operator, Operator):
    bl_idname = "bim.remove_drawing_style"
    bl_label = "Remove Drawing Style"
    bl_options = {"REGISTER", "UNDO"}
    index: bpy.props.IntProperty()

    def execute(self, context):
        context.scene.DocProperties.drawing_styles.remove(self.index)
        context.scene.camera.data.BIMCameraProperties.active_drawing_style_index = max(self.index - 1, 0)
        bpy.ops.bim.save_drawing_styles_data()
        return {"FINISHED"}


class SaveDrawingStyle(bpy.types.Operator, Operator):
    bl_idname = "bim.save_drawing_style"
    bl_label = "Save Drawing Style"
    bl_options = {"REGISTER", "UNDO"}
    bl_description = "Save current render setting to currently selected drawing style. Also resaves styles data to IFC"

    index: bpy.props.StringProperty()
    # TODO: check undo redo

    def execute(self, context):
        space = self.get_view_3d(context)  # Do not remove. It is used later in eval
        scene = context.scene
        style = {}
        eval_namespace = {"context": context, "scene": scene, "space": space}

        def add_prop_to_style(prop_path, context, scene, space):
            value = eval(prop_path)
            if not isinstance(value, str):
                try:
                    value = tuple(value)
                except TypeError:
                    pass
            style[prop_path] = value

        for prop in RasterStyleProperty:
            if prop.name.startswith("EVAL_PROP"):
                prop_path = prop.value
                add_prop_to_style(prop_path, **eval_namespace)
            else:
                props_source_path = prop.value
                props_source = eval(props_source_path)
                for prop_name in dir(props_source):
                    if prop_name.startswith("__"):
                        continue

                    prop_path = f"{props_source_path}.{prop_name}"
                    prop_value = eval(prop_path)
                    if (
                        not isinstance(prop_value, (int, float, bool, str, Color, Vector))
                        or props_source.is_property_readonly(prop_name)
                        or prop_path in RASTER_STYLE_PROPERTIES_EXCLUDE
                    ):
                        continue

                    add_prop_to_style(prop_path, **eval_namespace)

        if self.index:
            index = int(self.index)
        else:
            index = context.active_object.data.BIMCameraProperties.active_drawing_style_index
        scene.DocProperties.drawing_styles[index].raster_style = json.dumps(style)

        bpy.ops.bim.save_drawing_styles_data()
        return {"FINISHED"}

    def get_view_3d(self, context):
        for area in context.screen.areas:
            if area.type != "VIEW_3D":
                continue
            for space in area.spaces:
                if space.type != "VIEW_3D":
                    continue
                return space


class SaveDrawingStylesData(bpy.types.Operator, Operator):
    bl_idname = "bim.save_drawing_styles_data"
    bl_label = "Save Drawing Styles Data"
    bl_options = {"REGISTER", "UNDO"}

    skip_updating_current_style: bpy.props.BoolProperty(default=False)
    rename_style: bpy.props.BoolProperty(default=False)
    rename_style_from: bpy.props.StringProperty(default="")
    rename_style_to: bpy.props.StringProperty(default="")

    def execute(self, context):
        if not DrawingsData.is_loaded:
            DrawingsData.load()
        drawing_pset_data = DrawingsData.data["active_drawing_pset_data"]
        drawing_styles = context.scene.DocProperties.drawing_styles

        rel_path = drawing_pset_data["ShadingStyles"]
        current_style = drawing_pset_data.get("CurrentShadingStyle", None)
        json_path = Path(tool.Ifc.resolve_uri(rel_path))
        if not json_path.exists():
            self.report({"ERROR"}, "Shading styles file not found: {}".format(json_path))
            return {"CANCELLED"}

        styles_data = {}
        for style in drawing_styles:
            style_data = {"render_type": style.render_type, "raster_style": json.loads(style.raster_style)}
            styles_data[style.name] = style_data

        with open(json_path, "w") as fo:
            json.dump(styles_data, fo, indent=4)

        # TODO: currently it doesn't update current style for the other drawings
        # handling case when current style is not present in styles saved in ifc
        if not self.skip_updating_current_style and current_style not in styles_data and current_style is not None:
            # style was renamed
            if self.rename_style and current_style == self.rename_style_from:
                new_style_name = self.rename_style_to

            # style was removed
            else:
                new_style_name = None

            ifc_file = tool.Ifc.get()
            drawing = ifc_file.by_id(context.scene.DocProperties.active_drawing_id)
            pset = tool.Pset.get_element_pset(drawing, "EPset_Drawing")
            ifcopenshell.api.run(
                "pset.edit_pset", ifc_file, pset=pset, properties={"CurrentShadingStyle": new_style_name}
            )
            blenderbim.bim.handler.refresh_ui_data()

        return {"FINISHED"}


class ActivateDrawingStyle(bpy.types.Operator, Operator):
    bl_idname = "bim.activate_drawing_style"
    bl_label = "Activate Drawing Style"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        scene = context.scene
        ifc_file = tool.Ifc.get()
        active_drawing_style_index = scene.camera.data.BIMCameraProperties.active_drawing_style_index

        if active_drawing_style_index >= len(scene.DocProperties.drawing_styles):
            self.report({"ERROR"}, "Could not find active drawing style")
            return {"CANCELLED"}

        self.drawing_style = scene.DocProperties.drawing_styles[active_drawing_style_index]

        self.set_raster_style(context)
        self.set_query(context)

        drawing = ifc_file.by_id(scene.DocProperties.active_drawing_id)
        pset = tool.Pset.get_element_pset(drawing, "EPset_Drawing")
        ifcopenshell.api.run(
            "pset.edit_pset", ifc_file, pset=pset, properties={"CurrentShadingStyle": self.drawing_style.name}
        )
        blenderbim.bim.handler.refresh_ui_data()
        return {"FINISHED"}

    def set_raster_style(self, context):
        scene = context.scene  # Do not remove. It is used in exec later
        space = self.get_view_3d(context)  # Do not remove. It is used in exec later
        style = json.loads(self.drawing_style.raster_style)
        for path, value in style.items():
            try:
                if isinstance(value, str):
                    exec(f"{path} = '{value}'")
                else:
                    exec(f"{path} = {value}")
            except:
                # Differences in Blender versions mean result in failures here
                print(f"Failed to set shading style {path} to {value}")

    def set_query(self, context):
        self.include_global_ids = []
        self.exclude_global_ids = []
        for ifc_file in context.scene.DocProperties.ifc_files:
            try:
                ifc = ifcopenshell.open(ifc_file.name)
            except:
                continue
            if self.drawing_style.include_query:
                results = ifcopenshell.util.selector.Selector.parse(ifc, self.drawing_style.include_query)
                self.include_global_ids.extend([e.GlobalId for e in results])
            if self.drawing_style.exclude_query:
                results = ifcopenshell.util.selector.Selector.parse(ifc, self.drawing_style.exclude_query)
                self.exclude_global_ids.extend([e.GlobalId for e in results])
        if self.drawing_style.include_query:
            self.parse_filter_query("INCLUDE", context)
        if self.drawing_style.exclude_query:
            self.parse_filter_query("EXCLUDE", context)

    def parse_filter_query(self, mode, context):
        if mode == "INCLUDE":
            objects = context.scene.objects
        elif mode == "EXCLUDE":
            objects = context.visible_objects
        for obj in objects:
            if mode == "INCLUDE":
                obj.hide_viewport = False  # Note: this breaks alt-H
            global_id = obj.BIMObjectProperties.attributes.get("GlobalId")
            if not global_id:
                continue
            global_id = global_id.string_value
            if mode == "INCLUDE":
                if global_id not in self.include_global_ids:
                    obj.hide_viewport = True  # Note: this breaks alt-H
            elif mode == "EXCLUDE":
                if global_id in self.exclude_global_ids:
                    obj.hide_viewport = True  # Note: this breaks alt-H

    def get_view_3d(self, context):
        for area in context.screen.areas:
            if area.type != "VIEW_3D":
                continue
            for space in area.spaces:
                if space.type != "VIEW_3D":
                    continue
                return space


class RemoveSheet(bpy.types.Operator, Operator):
    bl_idname = "bim.remove_sheet"
    bl_label = "Remove Sheet"
    bl_options = {"REGISTER", "UNDO"}
    sheet: bpy.props.IntProperty()

    def _execute(self, context):
        core.remove_sheet(tool.Ifc, tool.Drawing, sheet=tool.Ifc.get().by_id(self.sheet))


class AddSchedule(bpy.types.Operator, Operator):
    bl_idname = "bim.add_schedule"
    bl_label = "Add Schedule"
    bl_options = {"REGISTER", "UNDO"}
    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    filter_glob: bpy.props.StringProperty(default="*.ods;*.xls;*.xlsx", options={"HIDDEN"})
    use_relative_path: bpy.props.BoolProperty(name="Use Relative Path", default=True)

    def _execute(self, context):
        filepath = self.filepath
        if self.use_relative_path:
            ifc_path = tool.Ifc.get_path()
            if os.path.isfile(ifc_path):
                ifc_path = os.path.dirname(ifc_path)

            # taking into account different drives on windows
            if Path(filepath).drive == Path(ifc_path).drive:
                filepath = os.path.relpath(filepath, ifc_path)
        core.add_document(
            tool.Ifc,
            tool.Drawing,
            "SCHEDULE",
            uri=filepath,
        )

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


class RemoveSchedule(bpy.types.Operator, Operator):
    bl_idname = "bim.remove_schedule"
    bl_label = "Remove Schedule"
    bl_options = {"REGISTER", "UNDO"}
    schedule: bpy.props.IntProperty()

    def _execute(self, context):
        core.remove_document(tool.Ifc, tool.Drawing, "SCHEDULE", document=tool.Ifc.get().by_id(self.schedule))


class OpenSchedule(bpy.types.Operator, Operator):
    bl_idname = "bim.open_schedule"
    bl_label = "Open Schedule"
    bl_options = {"REGISTER", "UNDO"}
    schedule: bpy.props.IntProperty()

    def _execute(self, context):
        core.open_schedule(tool.Drawing, schedule=tool.Ifc.get().by_id(self.schedule))


class BuildSchedule(bpy.types.Operator, Operator):
    bl_idname = "bim.build_schedule"
    bl_label = "Build Schedule"
    schedule: bpy.props.IntProperty()

    def _execute(self, context):
        core.build_schedule(tool.Drawing, schedule=tool.Ifc.get().by_id(self.schedule))


class AddScheduleToSheet(bpy.types.Operator, Operator):
    bl_idname = "bim.add_schedule_to_sheet"
    bl_label = "Add Schedule To Sheet"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        props = context.scene.DocProperties
        return props.schedules and props.sheets and context.scene.BIMProperties.data_dir

    def _execute(self, context):
        props = context.scene.DocProperties
        active_schedule = props.schedules[props.active_schedule_index]
        active_sheet = tool.Drawing.get_active_sheet(context)
        schedule = tool.Ifc.get().by_id(active_schedule.ifc_definition_id)
        if tool.Ifc.get_schema() == "IFC2X3":
            schedule_location = tool.Drawing.get_path_with_ext(schedule.DocumentReferences[0].Location, "svg")
        else:
            schedule_location = tool.Drawing.get_path_with_ext(schedule.HasDocumentReferences[0].Location, "svg")

        sheet = tool.Ifc.get().by_id(active_sheet.ifc_definition_id)
        if not sheet.is_a("IfcDocumentInformation"):
            return

        references = tool.Drawing.get_document_references(sheet)

        has_schedule = False
        for reference in references:
            if reference.Location == schedule_location:
                has_schedule = True
                break
        if has_schedule:
            return

        if not tool.Drawing.does_file_exist(tool.Ifc.resolve_uri(schedule_location)):
            self.report({"ERROR"}, "The schedule must be generated before adding to a sheet.")
            return

        reference = tool.Ifc.run("document.add_reference", information=sheet)
        id_attr = "ItemReference" if tool.Ifc.get_schema() == "IFC2X3" else "Identification"
        attributes = {
            id_attr: str(len([r for r in references if r.Description in ("DRAWING", "SCHEDULE")]) + 1),
            "Location": schedule_location,
            "Description": "SCHEDULE",
        }
        tool.Ifc.run("document.edit_reference", reference=reference, attributes=attributes)

        sheet_builder = sheeter.SheetBuilder()
        sheet_builder.data_dir = context.scene.BIMProperties.data_dir
        sheet_builder.add_document(reference, schedule, sheet)

        tool.Drawing.import_sheets()


class AddReferenceToSheet(bpy.types.Operator, Operator):
    bl_idname = "bim.add_reference_to_sheet"
    bl_label = "Add Reference To Sheet"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        props = context.scene.DocProperties
        return props.references and props.sheets and context.scene.BIMProperties.data_dir

    def _execute(self, context):
        props = context.scene.DocProperties
        active_reference = props.references[props.active_reference_index]
        active_sheet = tool.Drawing.get_active_sheet(context)
        extref = tool.Ifc.get().by_id(active_reference.ifc_definition_id)
        if tool.Ifc.get_schema() == "IFC2X3":
            extref_location = tool.Drawing.get_path_with_ext(extref.DocumentReferences[0].Location, "svg")
        else:
            extref_location = tool.Drawing.get_path_with_ext(extref.HasDocumentReferences[0].Location, "svg")

        sheet = tool.Ifc.get().by_id(active_sheet.ifc_definition_id)
        if not sheet.is_a("IfcDocumentInformation"):
            return

        references = tool.Drawing.get_document_references(sheet)

        has_extref = False
        for reference in references:
            if reference.Location == extref_location:
                has_extref = True
                break
        if has_extref:
            return

        if not tool.Drawing.does_file_exist(tool.Ifc.resolve_uri(extref_location)):
            self.report({"ERROR"}, f"Cannot find reference svg by path {extref_location}.")
            return

        reference = tool.Ifc.run("document.add_reference", information=sheet)
        id_attr = "ItemReference" if tool.Ifc.get_schema() == "IFC2X3" else "Identification"
        attributes = {
            id_attr: str(len([r for r in references if r.Description in ("DRAWING", "REFERENCE")]) + 1),
            "Location": extref_location,
            "Description": "REFERENCE",
        }
        tool.Ifc.run("document.edit_reference", reference=reference, attributes=attributes)

        sheet_builder = sheeter.SheetBuilder()
        sheet_builder.data_dir = context.scene.BIMProperties.data_dir
        sheet_builder.add_document(reference, extref, sheet)

        tool.Drawing.import_sheets()


class AddReference(bpy.types.Operator, Operator):
    bl_idname = "bim.add_reference"
    bl_label = "Add Reference"
    bl_options = {"REGISTER", "UNDO"}
    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    filter_glob: bpy.props.StringProperty(default="*.svg", options={"HIDDEN"})
    use_relative_path: bpy.props.BoolProperty(name="Use Relative Path", default=True)

    def _execute(self, context):
        filepath = self.filepath
        if self.use_relative_path:
            ifc_path = tool.Ifc.get_path()
            if os.path.isfile(ifc_path):
                ifc_path = os.path.dirname(ifc_path)

            # taking into account different drives on windows
            if Path(filepath).drive == Path(ifc_path).drive:
                filepath = os.path.relpath(filepath, ifc_path)
        core.add_document(
            tool.Ifc,
            tool.Drawing,
            "REFERENCE",
            uri=filepath,
        )

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


class RemoveReference(bpy.types.Operator, Operator):
    bl_idname = "bim.remove_reference"
    bl_label = "Remove Reference"
    bl_options = {"REGISTER", "UNDO"}
    reference: bpy.props.IntProperty()

    def _execute(self, context):
        core.remove_document(tool.Ifc, tool.Drawing, "REFERENCE", document=tool.Ifc.get().by_id(self.reference))


class OpenReference(bpy.types.Operator, Operator):
    bl_idname = "bim.open_reference"
    bl_label = "Open Reference"
    bl_options = {"REGISTER", "UNDO"}
    reference: bpy.props.IntProperty()

    def _execute(self, context):
        core.open_reference(tool.Drawing, reference=tool.Ifc.get().by_id(self.reference))


# TODO: dead code - drawing style attributes are never used?
class AddDrawingStyleAttribute(bpy.types.Operator):
    bl_idname = "bim.add_drawing_style_attribute"
    bl_label = "Add Drawing Style Attribute"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.camera.data.BIMCameraProperties
        context.scene.DocProperties.drawing_styles[props.active_drawing_style_index].attributes.add()
        return {"FINISHED"}


# TODO: dead code - drawing style attributes are never used?
class RemoveDrawingStyleAttribute(bpy.types.Operator):
    bl_idname = "bim.remove_drawing_style_attribute"
    bl_label = "Remove Drawing Style Attribute"
    bl_options = {"REGISTER", "UNDO"}
    index: bpy.props.IntProperty()

    def execute(self, context):
        props = context.scene.camera.data.BIMCameraProperties
        context.scene.DocProperties.drawing_styles[props.active_drawing_style_index].attributes.remove(self.index)
        return {"FINISHED"}


class CleanWireframes(bpy.types.Operator):
    bl_idname = "bim.clean_wireframes"
    bl_label = "Clean Wireframes"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        if context.selected_objects:
            objects = context.selected_objects
        else:
            objects = context.scene.objects
        for obj in (o for o in objects if o.type == "MESH"):
            if "EDGE_SPLIT" not in (m.type for m in obj.modifiers):
                obj.modifiers.new("EdgeSplit", "EDGE_SPLIT")
        return {"FINISHED"}


class EditTextPopup(bpy.types.Operator):
    bl_idname = "bim.edit_text_popup"
    bl_label = "Edit Text"
    first_run: bpy.props.BoolProperty(default=True)

    def draw(self, context):
        # shares most of the code with BIM_PT_text.draw()
        # need to keep them in sync or move to some common function
        # NOTE: that `popup_active_attribute` is used here when it's not used in `BIM_PT_text.draw()`

        props = context.active_object.BIMTextProperties

        row = self.layout.row(align=True)
        row.operator("bim.add_text_literal", icon="ADD", text="Add Literal")

        row = self.layout.row(align=True)
        row.prop(props, "font_size")

        for i, literal_props in enumerate(props.literals):
            box = self.layout.box()
            row = self.layout.row(align=True)

            row = box.row(align=True)
            row.label(text=f"Literal[{i}]:")
            row.operator("bim.remove_text_literal", icon="X", text="").literal_prop_id = i

            # skip BoxAlignment since we're going to format it ourselves
            attributes = [a for a in literal_props.attributes if a.name != "BoxAlignment"]
            blenderbim.bim.helper.draw_attributes(attributes, box, popup_active_attribute=attributes[0])

            row = box.row(align=True)
            cols = [row.column(align=True) for i in range(3)]
            for i in range(9):
                cols[i % 3].prop(
                    literal_props,
                    "box_alignment",
                    text="",
                    index=i,
                    icon="RADIOBUT_ON" if literal_props.box_alignment[i] else "RADIOBUT_OFF",
                )

            col = row.column(align=True)
            col.label(text="    Text box alignment:")
            col.label(text=f'    {literal_props.attributes["BoxAlignment"].string_value}')

    def cancel(self, context):
        # disable editing when dialog is closed
        bpy.ops.bim.disable_editing_text()

    def execute(self, context):
        # can't use invoke() because this operator
        # will be run indirectly by hotkey
        # so we use execute() and track whether it's the first run of the operator
        if self.first_run:
            bpy.ops.bim.enable_editing_text()
            self.first_run = False
            return context.window_manager.invoke_props_dialog(self)
        else:
            bpy.ops.bim.edit_text()
            return {"FINISHED"}


class EditText(bpy.types.Operator, Operator):
    bl_idname = "bim.edit_text"
    bl_label = "Edit Text"
    bl_options = {"REGISTER", "UNDO"}

    def _execute(self, context):
        core.edit_text(tool.Drawing, obj=context.active_object)
        tool.Blender.update_viewport()


class EnableEditingText(bpy.types.Operator, Operator):
    bl_idname = "bim.enable_editing_text"
    bl_label = "Enable Editing Text"
    bl_options = {"REGISTER", "UNDO"}

    def _execute(self, context):
        core.enable_editing_text(tool.Drawing, obj=context.active_object)


class DisableEditingText(bpy.types.Operator, Operator):
    bl_idname = "bim.disable_editing_text"
    bl_label = "Disable Editing Text"
    bl_options = {"REGISTER", "UNDO"}

    def _execute(self, context):
        obj = context.active_object
        core.disable_editing_text(tool.Drawing, obj=obj)

        # force update this object's font size for viewport display
        DecoratorData.data.pop(obj.name, None)
        tool.Drawing.update_text_value(obj)
        tool.Blender.update_viewport()


class AddTextLiteral(bpy.types.Operator):
    bl_idname = "bim.add_text_literal"
    bl_label = "Add Text Literal"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = context.active_object

        # similar to `tool.Drawing.import_text_attributes`
        literal_props = obj.BIMTextProperties.literals.add()
        literal_attributes = literal_props.attributes
        literal_attr_values = {
            "Literal": "Literal",
            "Path": "RIGHT",
            "BoxAlignment": "bottom_left",
        }
        # emulates `blenderbim.bim.helper.import_attributes2(ifc_literal, literal_props.attributes)`
        for attr_name in literal_attr_values:
            attr = literal_attributes.add()
            attr.name = attr_name
            if attr_name == "Path":
                attr.data_type = "enum"
                attr.enum_items = '["DOWN", "LEFT", "RIGHT", "UP"]'
                attr.enum_value = literal_attr_values[attr_name]

            else:
                attr.data_type = "string"
                attr.string_value = literal_attr_values[attr_name]

        box_alignment_mask = [False] * 9
        box_alignment_mask[6] = True  # bottom_left box_alignment
        literal_props.box_alignment = box_alignment_mask
        return {"FINISHED"}


class RemoveTextLiteral(bpy.types.Operator):
    bl_idname = "bim.remove_text_literal"
    bl_label = "Remove Text Literal"
    bl_options = {"REGISTER", "UNDO"}

    literal_prop_id: bpy.props.IntProperty()

    def execute(self, context):
        obj = context.active_object
        obj.BIMTextProperties.literals.remove(self.literal_prop_id)
        return {"FINISHED"}


class AssignSelectedObjectAsProduct(bpy.types.Operator):
    bl_idname = "bim.assign_selected_as_product"
    bl_label = "Assign Selected Object As Product"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        if len(context.selected_objects) != 2:
            cls.poll_message_set("2 objects need to be selected")
            return False
        return True

    def execute(self, context):
        objs = context.selected_objects[:]
        obj1 = objs[0]
        element1 = tool.Ifc.get_entity(obj1)
        obj2 = objs[1]
        element2 = tool.Ifc.get_entity(obj2)
        if element1.is_a("IfcAnnotation"):
            other_selected_object = obj2
            bpy.context.view_layer.objects.active = obj1
        elif element2.is_a("IfcAnnotation"):
            other_selected_object = obj1
            bpy.context.view_layer.objects.active = obj2
        context.active_object.BIMAssignedProductProperties.relating_product = other_selected_object
        bpy.ops.bim.edit_assigned_product()
        return {"FINISHED"}


class EditAssignedProduct(bpy.types.Operator, Operator):
    bl_idname = "bim.edit_assigned_product"
    bl_label = "Edit Text Product"
    bl_options = {"REGISTER", "UNDO"}

    def _execute(self, context):
        product = None
        if context.active_object.BIMAssignedProductProperties.relating_product:
            product = tool.Ifc.get_entity(context.active_object.BIMAssignedProductProperties.relating_product)
        core.edit_assigned_product(tool.Ifc, tool.Drawing, obj=context.active_object, product=product)
        tool.Blender.update_viewport()


class EnableEditingAssignedProduct(bpy.types.Operator, Operator):
    bl_idname = "bim.enable_editing_assigned_product"
    bl_label = "Enable Editing Assigned Product"
    bl_options = {"REGISTER", "UNDO"}

    def _execute(self, context):
        core.enable_editing_assigned_product(tool.Drawing, obj=context.active_object)


class DisableEditingAssignedProduct(bpy.types.Operator, Operator):
    bl_idname = "bim.disable_editing_assigned_product"
    bl_label = "Disable Editing Assigned Product"
    bl_options = {"REGISTER", "UNDO"}

    def _execute(self, context):
        core.disable_editing_assigned_product(tool.Drawing, obj=context.active_object)


class LoadSheets(bpy.types.Operator, Operator):
    bl_idname = "bim.load_sheets"
    bl_label = "Load Sheets"
    bl_options = {"REGISTER", "UNDO"}

    def _execute(self, context):
        core.load_sheets(tool.Drawing)

        props = context.scene.DocProperties
        sheets_not_found = []
        for sheet_prop in props.sheets:
            if not sheet_prop.is_sheet:
                continue

            sheet = tool.Ifc.get().by_id(sheet_prop.ifc_definition_id)
            document_uri = tool.Drawing.get_document_uri(sheet)

            filepath = Path(document_uri)
            if not filepath.is_file():
                sheet_name = f"{sheet_prop.identification} - {sheet_prop.name}"
                sheets_not_found.append(f'"{sheet_name}" - {document_uri}')

        if sheets_not_found:
            self.report({"ERROR"}, "Some sheets svg files are missing:\n" + "\n".join(sheets_not_found))


class EditSheet(bpy.types.Operator, Operator):
    bl_idname = "bim.edit_sheet"
    bl_label = "Edit Sheet"
    bl_options = {"REGISTER", "UNDO"}
    identification: bpy.props.StringProperty()
    name: bpy.props.StringProperty()

    def invoke(self, context, event):
        self.props = context.scene.DocProperties
        sheet = tool.Ifc.get().by_id(self.props.sheets[self.props.active_sheet_index].ifc_definition_id)
        if sheet.is_a("IfcDocumentInformation"):
            self.document_type = "SHEET"
            self.name = sheet.Name
            self.identification = sheet.Identification
        elif sheet.is_a("IfcDocumentReference") and sheet.Description == "TITLEBLOCK":
            self.document_type = "TITLEBLOCK"
        else:
            self.document_type = "EMBEDDED"
            self.identification = sheet.Identification
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        if self.document_type == "SHEET":
            row = self.layout.row()
            row.prop(self, "identification", text="Identification")
            row = self.layout.row()
            row.prop(self, "name", text="Name")
        elif self.document_type == "TITLEBLOCK":
            row = self.layout.row()
            row.prop(context.scene.DocProperties, "titleblock", text="Titleblock")
        elif self.document_type == "EMBEDDED":
            row = self.layout.row()
            row.prop(self, "identification", text="Identification")

    def _execute(self, context):
        self.props = context.scene.DocProperties
        sheet = tool.Ifc.get().by_id(self.props.sheets[self.props.active_sheet_index].ifc_definition_id)
        if self.document_type == "SHEET":
            core.rename_sheet(tool.Ifc, tool.Drawing, sheet=sheet, identification=self.identification, name=self.name)
        elif self.document_type == "EMBEDDED":
            core.rename_reference(tool.Ifc, reference=sheet, identification=self.identification)
        elif self.document_type == "TITLEBLOCK":
            titleblock = self.props.titleblock
            reference = sheet
            sheet = tool.Drawing.get_reference_document(reference)
            tool.Ifc.run(
                "document.edit_reference",
                reference=reference,
                attributes={"Location": tool.Drawing.get_default_titleblock_path(titleblock)},
            )
            sheet_builder = sheeter.SheetBuilder()
            sheet_builder.data_dir = context.scene.BIMProperties.data_dir
            sheet_builder.change_titleblock(sheet, titleblock)
        tool.Drawing.import_sheets()


class DisableEditingSheets(bpy.types.Operator, Operator):
    bl_idname = "bim.disable_editing_sheets"
    bl_label = "Disable Editing Sheets"
    bl_options = {"REGISTER", "UNDO"}

    def _execute(self, context):
        core.disable_editing_sheets(tool.Drawing)


class LoadSchedules(bpy.types.Operator, Operator):
    bl_idname = "bim.load_schedules"
    bl_label = "Load Schedules"
    bl_options = {"REGISTER", "UNDO"}

    def _execute(self, context):
        core.load_schedules(tool.Drawing)


class DisableEditingSchedules(bpy.types.Operator, Operator):
    bl_idname = "bim.disable_editing_schedules"
    bl_label = "Disable Editing Schedules"
    bl_options = {"REGISTER", "UNDO"}

    def _execute(self, context):
        core.disable_editing_schedules(tool.Drawing)


class LoadReferences(bpy.types.Operator, Operator):
    bl_idname = "bim.load_references"
    bl_label = "Load References"
    bl_options = {"REGISTER", "UNDO"}

    def _execute(self, context):
        core.load_references(tool.Drawing)


class DisableEditingReferences(bpy.types.Operator, Operator):
    bl_idname = "bim.disable_editing_references"
    bl_label = "Disable Editing References"
    bl_options = {"REGISTER", "UNDO"}

    def _execute(self, context):
        core.disable_editing_references(tool.Drawing)


class LoadDrawings(bpy.types.Operator, Operator):
    bl_idname = "bim.load_drawings"
    bl_label = "Load Drawings"
    bl_options = {"REGISTER", "UNDO"}

    def _execute(self, context):
        core.load_drawings(tool.Drawing)


class DisableEditingDrawings(bpy.types.Operator, Operator):
    bl_idname = "bim.disable_editing_drawings"
    bl_label = "Disable Editing Drawings"
    bl_options = {"REGISTER", "UNDO"}

    def _execute(self, context):
        core.disable_editing_drawings(tool.Drawing)


class ExpandTargetView(bpy.types.Operator):
    bl_idname = "bim.expand_target_view"
    bl_label = "Expand Target View"
    bl_options = {"REGISTER", "UNDO"}
    target_view: bpy.props.StringProperty()

    def execute(self, context):
        props = context.scene.DocProperties
        for drawing in [d for d in props.drawings if d.target_view == self.target_view]:
            drawing.is_expanded = True
        core.load_drawings(tool.Drawing)
        return {"FINISHED"}


class ContractTargetView(bpy.types.Operator):
    bl_idname = "bim.contract_target_view"
    bl_label = "Contract Target View"
    bl_options = {"REGISTER", "UNDO"}
    target_view: bpy.props.StringProperty()

    def execute(self, context):
        props = context.scene.DocProperties
        for drawing in [d for d in props.drawings if d.target_view == self.target_view]:
            drawing.is_expanded = False
        core.load_drawings(tool.Drawing)
        return {"FINISHED"}


class ExpandSheet(bpy.types.Operator):
    bl_idname = "bim.expand_sheet"
    bl_label = "Expand Sheet"
    bl_options = {"REGISTER", "UNDO"}
    sheet: bpy.props.IntProperty()

    def execute(self, context):
        props = context.scene.DocProperties
        for sheet in [s for s in props.sheets if s.ifc_definition_id == self.sheet]:
            sheet.is_expanded = True
        core.load_sheets(tool.Drawing)
        return {"FINISHED"}


class ContractSheet(bpy.types.Operator):
    bl_idname = "bim.contract_sheet"
    bl_label = "Contract Sheet"
    bl_options = {"REGISTER", "UNDO"}
    sheet: bpy.props.IntProperty()

    def execute(self, context):
        props = context.scene.DocProperties
        for sheet in [s for s in props.sheets if s.ifc_definition_id == self.sheet]:
            sheet.is_expanded = False
        core.load_sheets(tool.Drawing)
        return {"FINISHED"}


class SelectAssignedProduct(bpy.types.Operator, Operator):
    bl_idname = "bim.select_assigned_product"
    bl_label = "Select Assigned Product"
    bl_options = {"REGISTER", "UNDO"}

    def _execute(self, context):
        core.select_assigned_product(tool.Drawing, context)


class EnableEditingElementFilter(bpy.types.Operator, Operator):
    bl_idname = "bim.enable_editing_element_filter"
    bl_label = "Enable Editing Element Filter"
    bl_options = {"REGISTER", "UNDO"}
    filter_mode: bpy.props.StringProperty()

    def _execute(self, context):
        obj = bpy.context.scene.camera
        if obj:
            obj.data.BIMCameraProperties.filter_mode = self.filter_mode


class EditElementFilter(bpy.types.Operator, Operator):
    bl_idname = "bim.edit_element_filter"
    bl_label = "Edit Element Filter"
    bl_options = {"REGISTER", "UNDO"}
    filter_mode: bpy.props.StringProperty()

    def _execute(self, context):
        props = context.active_object.data.BIMCameraProperties
        obj = bpy.context.scene.camera
        element = tool.Ifc.get_entity(obj)
        pset = tool.Pset.get_element_pset(element, "EPset_Drawing")
        if self.filter_mode == "INCLUDE":
            query = tool.Search.export_filter_query(props.include_filter_groups) or None
            ifcopenshell.api.run("pset.edit_pset", tool.Ifc.get(), pset=pset, properties={"Include": query})
        elif self.filter_mode == "EXCLUDE":
            query = tool.Search.export_filter_query(props.exclude_filter_groups) or None
            ifcopenshell.api.run("pset.edit_pset", tool.Ifc.get(), pset=pset, properties={"Exclude": query})
        obj.data.BIMCameraProperties.filter_mode = "NONE"
        bpy.ops.bim.activate_drawing(drawing=element.id(), camera_view_point=False)


class AddReferenceImage(bpy.types.Operator, Operator):
    bl_idname = "bim.add_reference_image"
    bl_label = "Add Reference Image"
    bl_options = {"REGISTER", "UNDO"}

    use_relative_path: bpy.props.BoolProperty(name="Use Relative Path", default=True)
    filepath: bpy.props.StringProperty(
        name="File Path", description="Filepath used to import from", maxlen=1024, default="", subtype="FILE_PATH"
    )
    filter_image: bpy.props.BoolProperty(default=True, options={"HIDDEN", "SKIP_SAVE"})
    filter_folder: bpy.props.BoolProperty(default=True, options={"HIDDEN", "SKIP_SAVE"})

    override_existing_image: bpy.props.BoolProperty(
        name="Override Existing Image",
        default=True,
        description=(
            "Override image if it was previously loaded to Blender. If disabled, will always create a new image"
        ),
    )
    use_existing_object_by_name: bpy.props.StringProperty(
        name="Use Existing Object By Name",
        description="Existing object name to add a style with reference image to. If not provided will create a new object.",
        options={"SKIP_SAVE"},
    )

    def draw(self, context):
        layout = self.layout
        if Path(tool.Ifc.get_path()).is_file():
            layout.prop(self, "use_relative_path")
        else:
            self.use_relative_path = False
            layout.label(text="Save the .ifc file first ")
            layout.label(text="to use relative paths.")
        layout.prop(self, "override_existing_image")
        layout.prop(self, "use_existing_object_by_name")

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def _execute(self, context):
        abs_path = Path(self.filepath)
        if self.use_relative_path:
            image_filepath = abs_path.relative_to(Path(tool.Ifc.get_path()).parent)
        else:
            image_filepath = abs_path
        ifc_file = tool.Ifc.get()

        if self.override_existing_image:
            params = {"check_existing": True, "force_reload": True}
        else:
            params = {"check_existing": False}
        image = load_image(abs_path.name, abs_path.parent, **params)

        def bm_add_image_plane(mesh):
            bm = tool.Blender.get_bmesh_for_mesh(mesh, clean=True)
            plane_scale = (Vector(image.size) / min(image.size)).to_3d()
            matrix = Matrix.LocRotScale(None, None, plane_scale)
            bmesh.ops.create_grid(bm, x_segments=1, y_segments=1, size=1, matrix=matrix, calc_uvs=False)
            tool.Blender.apply_bmesh(mesh, bm)

        if self.use_existing_object_by_name:
            obj = bpy.data.objects[self.use_existing_object_by_name]
            bm_add_image_plane(obj.data)
            bpy.ops.bim.update_representation(obj=obj.name, ifc_representation_class="")
        else:
            temp_mesh = bpy.data.meshes.new("temp_mesh")
            bm_add_image_plane(temp_mesh)
            obj = bpy.data.objects.new(image_filepath.stem, temp_mesh)
            tool.Drawing.run_root_assign_class(
                obj=obj,
                ifc_class="IfcAnnotation",
                predefined_type="IMAGE",
                should_add_representation=True,
                context=ifcopenshell.util.representation.get_context(ifc_file, "Model", "Body", "MODEL_VIEW"),
                ifc_representation_class=None,
            )
            tool.Blender.remove_data_block(temp_mesh)

        tool.Blender.set_active_object(obj)

        material = bpy.data.materials.new(name=image_filepath.stem)
        obj.data.materials.append(None)  # new slot
        obj.material_slots[0].material = material
        bpy.ops.bim.add_style()

        style = ifc_file.by_id(material.BIMMaterialProperties.ifc_style_id)
        tool.Style.assign_style_to_object(style, obj)

        # TODO: IfcSurfaceStyleRendering is unnecessary here, added it only because
        # we don't support IfcSurfaceStyleWithTextures without Rendering yet
        shading_attributes = {
            "SurfaceColour": {
                "Red": 1.0,
                "Green": 1.0,
                "Blue": 1.0,
            },
            "Transparency": 0.0,
            "ReflectanceMethod": "NOTDEFINED",
        }
        ifcopenshell.api.run(
            "style.add_surface_style",
            tool.Ifc.get(),
            style=style,
            ifc_class="IfcSurfaceStyleRendering",
            attributes=shading_attributes,
        )
        texture = ifc_file.create_entity("IfcImageTexture", Mode="DIFFUSE", URLReference=image_filepath.as_posix())
        textures = [texture]
        ifc_file.create_entity("IfcTextureCoordinateGenerator", Maps=textures, Mode="COORD")  # UV map
        tool.Ifc.run(
            "style.add_surface_style",
            style=style,
            ifc_class="IfcSurfaceStyleWithTextures",
            attributes={"Textures": textures},
        )
        tool.Style.reload_material_from_ifc(material)
        tool.Geometry.record_object_materials(obj)


class ConvertSVGToDXF(bpy.types.Operator):
    bl_idname = "bim.convert_svg_to_dxf"
    bl_label = "Convert SVG to DXF"
    bl_options = {"REGISTER", "UNDO"}
    view: bpy.props.StringProperty()
    bl_description = "Convert current drawing's .svg to .dxf.\n\nSHIFT+CLICK to convert all selected drawings"
    convert_all: bpy.props.BoolProperty(name="Convert All", default=False, options={"SKIP_SAVE"})

    def invoke(self, context, event):
        # convert all drawings on shift+click
        # make sure to use SKIP_SAVE on property, otherwise it might get stuck
        if event.type == "LEFTMOUSE" and event.shift:
            self.open_all = True
        return self.execute(context)

    def execute(self, context):
        if self.convert_all:
            drawings = [
                tool.Ifc.get().by_id(d.ifc_definition_id) for d in context.scene.DocProperties.drawings if d.is_selected
            ]
        else:
            drawings = [tool.Ifc.get().by_id(context.scene.DocProperties.drawings.get(self.view).ifc_definition_id)]

        drawing_uris: list[Path] = []
        drawings_not_found: list[str] = []

        for drawing in drawings:
            drawing_uri = tool.Drawing.get_document_uri(tool.Drawing.get_drawing_document(drawing))
            if drawing_uri is None or not os.path.exists(drawing_uri):
                drawings_not_found.append(drawing.Name)
            else:
                drawing_uris.append(Path(drawing_uri))

        if drawings_not_found:
            msg = "Some drawings .svg files were not found, need to print them first: \n{}.".format(
                "\n".join(drawings_not_found)
            )
            self.report({"ERROR"}, msg)
            return {"CANCELLED"}

        for drawing_uri in drawing_uris:
            tool.Drawing.convert_svg_to_dxf(drawing_uri, drawing_uri.with_suffix(".dxf"))

        self.report({"INFO"}, f"{len(drawing_uris)} drawings were converted to .dxf.")
        return {"FINISHED"}

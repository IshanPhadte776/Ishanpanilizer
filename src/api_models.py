from pydantic import BaseModel, Field


class PanelizerSettings(BaseModel):
    panel_width: float = Field(default=1.2, gt=0)
    panel_height: float = Field(default=2.4, gt=0)
    cost_per_unique_panel_type: float = Field(default=250.0, ge=0)
    cost_per_panel_element: float = Field(default=45.0, ge=0)
    # None keeps the plain aligned grid; any int is a reproducible re-rolled layout
    seed: int | None = None
    origin_jitter: float = Field(default=0.3, ge=0, le=1)
    stagger: float = Field(default=0.0, ge=0, le=1)
    # Below-grade walls are tagged (is_basement), not dropped, at extraction time --
    # default False matches the old behaviour of basements never being panelizable.
    include_basement: bool = False


class PanelizeRequest(BaseModel):
    input_json: str = "input/outputID2/Output/lod3.json"
    output_json: str | None = None
    selected_building_indices: list[int] | None = [0]
    settings: PanelizerSettings = Field(default_factory=PanelizerSettings)
    include_building: bool = True


class BuildingRequest(BaseModel):
    input_json: str = "input/outputID2/Output/lod3.json"
    selected_building_indices: list[int] | None = [0]

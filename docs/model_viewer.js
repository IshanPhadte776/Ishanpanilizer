import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";

const startedAt = performance.now();

const params = new URLSearchParams(location.search);
const src = params.get("src") ?? "model.glb";
const label = params.get("label");
document.title = label ? `${label} - model viewer` : "Model viewer";

const hudTitle = document.querySelector("#hud-title");
const hudSub = document.querySelector("#hud-sub");
const hudStats = document.querySelector("#hud-stats");

function formatMs(ms) {
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(2)} s`;
}

// Timings are taken from when this module starts executing: fetchMs covers downloading and
// parsing the GLB, firstFrameMs runs through to the frame where it is actually on screen
// (which is where GPU upload of the buffers really lands).
let fetchMs = 0;
let awaitingFirstFrame = false;

const canvas = document.querySelector("#scene");
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.75));

const scene = new THREE.Scene();
scene.background = new THREE.Color("#f5f7f9");

const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 5000);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;

scene.add(new THREE.AmbientLight("#ffffff", 0.85));
const sun = new THREE.DirectionalLight("#ffffff", 1.15);
sun.position.set(30, -45, 60);
scene.add(sun);

new GLTFLoader().load(
  src,
  (gltf) => {
    fetchMs = performance.now() - startedAt;
    const root = gltf.scene;

    let meshes = 0;
    root.traverse((object) => {
      if (!object.isMesh) return;
      meshes += 1;
      const materials = Array.isArray(object.material) ? object.material : [object.material];
      for (const material of materials) {
        material.side = THREE.DoubleSide;
        // a GLB with no material gets glTF's default metallic=1, which renders solid
        // black with no environment map -- force it dielectric
        if (material.isMeshStandardMaterial) {
          material.metalness = 0;
          if (material.roughness === undefined || material.roughness === 1) material.roughness = 0.85;
        }
        // fall back to baked vertex colours when that's all the file carries
        if (object.geometry.hasAttribute("color")) material.vertexColors = true;
        material.needsUpdate = true;
      }
    });

    // z-up (IFC/CityJSON) -> three.js y-up, then recentre on the origin
    root.rotation.x = -Math.PI / 2;
    scene.add(root);

    const box = new THREE.Box3().setFromObject(root);
    const size = box.getSize(new THREE.Vector3());
    const center = box.getCenter(new THREE.Vector3());

    // Only recentre horizontally. Vertically, y=0 is meaningful as-authored: the export
    // step (scripts/view_model.py) already places grade level at z=0 when it can detect a
    // basement, so a below-grade storey renders below the grid rather than sitting on it;
    // for everything else it places the model's lowest point at z=0, which is the same
    // "sit on the grid" result this used to do here. Re-snapping min.y to 0 in this file
    // would undo that and drag any basement back up onto the grid.
    root.position.x -= center.x;
    root.position.z -= center.z;

    const span = Math.max(size.x, size.z) || 10;
    scene.add(new THREE.GridHelper(span * 2, 40, "#9aa5b1", "#d4dae2"));

    camera.position.set(span * 0.7, Math.max(size.y * 1.6, span * 0.4), span * 0.8);
    // keep near/far tight -- a huge ratio costs depth precision and z-fights coplanar faces
    camera.near = Math.max(0.05, span / 200);
    camera.far = span * 20;
    camera.updateProjectionMatrix();
    // look roughly 40% of the way up the above-grade portion (box.max.y is the roofline;
    // 0 is grade) -- identical to the old target when the model's base is already at 0
    controls.target.set(0, THREE.MathUtils.lerp(0, box.max.y, 0.4), 0);
    controls.update();

    hudTitle.textContent = label ?? src;
    hudSub.textContent =
      `${src} · ${meshes} meshes · ${size.x.toFixed(1)} × ${size.y.toFixed(1)} × ${size.z.toFixed(1)} m`;
    hudStats.innerHTML = `<strong>Loaded in ${formatMs(fetchMs)}</strong><br>rendering…`;
    awaitingFirstFrame = true;
  },
  (event) => {
    // 6.5MB of GLB is a visible wait, so show progress rather than a static "Loading…"
    if (event.lengthComputable && event.total) {
      hudTitle.textContent = `Loading… ${Math.round((event.loaded / event.total) * 100)}%`;
    } else {
      hudTitle.textContent = `Loading… ${(event.loaded / 1048576).toFixed(1)} MB`;
    }
  },
  (error) => {
    hudTitle.textContent = "Failed to load " + src;
    hudSub.textContent = String(error);
  },
);

resize();
renderer.setAnimationLoop(() => {
  controls.update();
  renderer.render(scene, camera);

  if (awaitingFirstFrame) {
    awaitingFirstFrame = false;
    const firstFrameMs = performance.now() - startedAt;
    // read straight after a render: renderer.info.render reports that frame's real counts,
    // which is the ground truth for whether the export-side mesh merging actually landed
    const { calls, triangles } = renderer.info.render;
    hudStats.innerHTML =
      `<strong>Loaded in ${formatMs(fetchMs)}</strong> · on screen at ${formatMs(firstFrameMs)}<br>` +
      `${calls} draw call${calls === 1 ? "" : "s"} · ${triangles.toLocaleString()} triangles`;
  }
});
window.addEventListener("resize", resize);

function resize() {
  const { clientWidth, clientHeight } = canvas;
  camera.aspect = clientWidth / clientHeight || 1;
  camera.updateProjectionMatrix();
  renderer.setSize(clientWidth, clientHeight, false);
}

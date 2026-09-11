import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";

const src = new URLSearchParams(location.search).get("src") ?? "model.glb";

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

    // sit the model ON the grid rather than centring it vertically: centring puts the
    // grid plane at mid-building height, where it slices across the model and its lines
    // read as streaks over the roofs
    root.position.x -= center.x;
    root.position.z -= center.z;
    root.position.y -= box.min.y;

    const span = Math.max(size.x, size.z) || 10;
    scene.add(new THREE.GridHelper(span * 2, 40, "#9aa5b1", "#d4dae2"));

    camera.position.set(span * 0.7, Math.max(size.y * 1.6, span * 0.4), span * 0.8);
    // keep near/far tight -- a huge ratio costs depth precision and z-fights coplanar faces
    camera.near = Math.max(0.05, span / 200);
    camera.far = span * 20;
    camera.updateProjectionMatrix();
    controls.target.set(0, size.y * 0.4, 0);
    controls.update();

    document.querySelector("#hud-title").textContent = src;
    document.querySelector("#hud-sub").textContent =
      `${meshes} meshes · ${size.x.toFixed(1)} × ${size.y.toFixed(1)} × ${size.z.toFixed(1)} m`;
  },
  undefined,
  (error) => {
    document.querySelector("#hud-title").textContent = "Failed to load " + src;
    document.querySelector("#hud-sub").textContent = String(error);
  },
);

resize();
renderer.setAnimationLoop(() => {
  controls.update();
  renderer.render(scene, camera);
});
window.addEventListener("resize", resize);

function resize() {
  const { clientWidth, clientHeight } = canvas;
  camera.aspect = clientWidth / clientHeight || 1;
  camera.updateProjectionMatrix();
  renderer.setSize(clientWidth, clientHeight, false);
}

import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";

const params = new URLSearchParams(location.search);
const sources = [
  { src: params.get("left") ?? "model_ifc.glb", label: params.get("leftLabel") ?? "left", side: "left" },
  { src: params.get("right") ?? "model_cityjson.glb", label: params.get("rightLabel") ?? "right", side: "right" },
];

// one camera drives both panes, so the two models are always seen from the same angle
const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 5000);
const controls = new OrbitControls(camera, document.querySelector("#wrap"));
controls.enableDamping = true;

const panes = sources.map(({ src, label, side }) => {
  const canvas = document.querySelector(`#${side}`);
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.75));

  const scene = new THREE.Scene();
  scene.background = new THREE.Color("#f5f7f9");
  scene.add(new THREE.AmbientLight("#ffffff", 0.85));
  const sun = new THREE.DirectionalLight("#ffffff", 1.15);
  sun.position.set(30, -45, 60);
  scene.add(sun);

  document.querySelector(`#tag-${side}`).textContent = label;
  return { src, label, side, renderer, scene, span: 10 };
});

await Promise.all(panes.map(loadInto));
frameCamera();

renderer_loop();

async function loadInto(pane) {
  const loader = new GLTFLoader();
  let gltf;
  try {
    gltf = await loader.loadAsync(pane.src);
  } catch (error) {
    document.querySelector(`#tag-${pane.side}`).textContent = `Failed to load ${pane.src}`;
    document.querySelector(`#sub-${pane.side}`).textContent = String(error);
    return;
  }

  const root = gltf.scene;
  let meshes = 0;
  root.traverse((object) => {
    if (!object.isMesh) return;
    meshes += 1;
    const materials = Array.isArray(object.material) ? object.material : [object.material];
    for (const material of materials) {
      material.side = THREE.DoubleSide;
      // glTF's default material is metallic=1, which renders black with no environment map
      if (material.isMeshStandardMaterial) {
        material.metalness = 0;
        if (material.roughness === undefined || material.roughness === 1) material.roughness = 0.85;
      }
      if (object.geometry.hasAttribute("color")) material.vertexColors = true;
      material.needsUpdate = true;
    }
  });

  root.rotation.x = -Math.PI / 2;
  pane.scene.add(root);

  // normalise both models the same way (centred in plan, sitting on the grid) so the
  // shared camera frames them identically and differences are actually comparable
  const box = new THREE.Box3().setFromObject(root);
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());
  root.position.x -= center.x;
  root.position.z -= center.z;
  root.position.y -= box.min.y;

  pane.span = Math.max(size.x, size.z) || 10;
  pane.scene.add(new THREE.GridHelper(pane.span * 2, 40, "#9aa5b1", "#d4dae2"));
  pane.height = size.y;

  document.querySelector(`#sub-${pane.side}`).textContent =
    `${pane.src} · ${meshes} meshes · ${size.x.toFixed(1)} × ${size.y.toFixed(1)} × ${size.z.toFixed(1)} m`;
}

function frameCamera() {
  const span = Math.max(...panes.map((p) => p.span));
  const height = Math.max(...panes.map((p) => p.height ?? 0));
  camera.position.set(span * 0.7, Math.max(height * 1.6, span * 0.4), span * 0.8);
  camera.near = Math.max(0.05, span / 200);
  camera.far = span * 20;
  camera.updateProjectionMatrix();
  controls.target.set(0, height * 0.4, 0);
  controls.update();
}

function renderer_loop() {
  panes[0].renderer.setAnimationLoop(() => {
    controls.update();
    for (const pane of panes) {
      const { clientWidth, clientHeight } = pane.renderer.domElement;
      if (!clientWidth || !clientHeight) continue;
      pane.renderer.setSize(clientWidth, clientHeight, false);
      camera.aspect = clientWidth / clientHeight;
      camera.updateProjectionMatrix();
      pane.renderer.render(pane.scene, camera);
    }
  });
}

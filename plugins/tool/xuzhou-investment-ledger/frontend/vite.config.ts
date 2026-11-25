export default {
  build: {
    lib: {
      entry: new URL("./src/index.ts", import.meta.url).pathname,
      formats: ["es"],
      fileName: () => "index.js",
    },
    outDir: new URL("./dist", import.meta.url).pathname,
    emptyOutDir: true,
    rollupOptions: {
      external: ["react", "react-dom"],
    },
  },
};

// This file configures Vite, a build tool for modern web development.
// It sets up the project to use React and specifies how the development server should run.

import { defineConfig } from "vite"; // Imports the function to define the configuration
import react from "@vitejs/plugin-react"; // Imports the plugin to support React

export default defineConfig({
  plugins: [react()], // Configures Vite to use the React plugin
  server: {
    port: 5173, // Sets the port number for the development server
  },
});
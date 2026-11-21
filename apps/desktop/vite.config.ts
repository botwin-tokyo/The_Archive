import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
export default defineConfig({root:'apps/desktop',base:'./',plugins:[react(),{name:'development-only-csp',transformIndexHtml(html,context){return context.server?html.replace("script-src 'self'", "script-src 'self' 'unsafe-inline'").replace("connect-src 'none'", "connect-src 'self' ws://127.0.0.1:4173"):html;}}],build:{outDir:'dist',emptyOutDir:true},server:{host:'127.0.0.1',port:4173,strictPort:true}});

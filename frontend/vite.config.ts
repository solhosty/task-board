import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import path from 'node:path';
export default defineConfig({plugins:[react(),tailwindcss()],base:'/',resolve:{alias:{'@':path.resolve(import.meta.dirname,'src')}},build:{outDir:'../static',emptyOutDir:false},server:{proxy:{'/api':{target:'http://127.0.0.1:4173',configure(proxy){proxy.on('proxyReq',request=>request.setHeader('origin','http://127.0.0.1:4173'));}}}}});

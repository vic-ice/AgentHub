import path from "path"
import type { PluginOption } from "vite"
import tailwindcss from "@tailwindcss/vite"
import { defineConfig, loadEnv } from "vite"
import react from "@vitejs/plugin-react"

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  // Load env file based on `mode` in the current working directory.
  const env = loadEnv(mode, process.cwd(), "")

  // Backend API proxy target for vite dev server
  // Can be configured via VITE_DEV_PROXY_TARGET in .env file
  const proxyTarget = env.VITE_DEV_PROXY_TARGET || "http://127.0.0.1:8080"

  console.log("[Vite Proxy] Target:", proxyTarget)

  return {
    root: __dirname,
    plugins: [react(), tailwindcss()] as PluginOption[],
    resolve: {
      alias: {
        "@": path.resolve(__dirname, "./src"),
      },
    },
    build: {
      rolldownOptions: {
        input: {
          index: path.resolve(__dirname, "index.html"),
        },
        output: {
          codeSplitting: {
            minSize: 20 * 1024,
            maxSize: 420 * 1024,
            groups: [
              {
                name: "react-core",
                test: /node_modules[\\/](react|react-dom|scheduler)[\\/]/,
                priority: 40,
              },
              {
                name: "markdown",
                test: /node_modules[\\/](marked|react-markdown|remark-|rehype-|unified|shiki|@shikijs)[\\/]/,
                priority: 30,
                maxSize: 420 * 1024,
              },
              {
                name: "ui-vendor",
                test: /node_modules[\\/](radix-ui|@radix-ui|lucide-react)[\\/]/,
                priority: 20,
                maxSize: 380 * 1024,
              },
              {
                name: "vendor",
                test: /node_modules[\\/]/,
                priority: 1,
                maxSize: 420 * 1024,
              },
            ],
          },
        },
      },
      // A few Shiki grammar modules are individually large but lazy-loaded
      // only when their language appears. Keep warnings focused on eagerly
      // loaded application chunks, which are split below this threshold.
      chunkSizeWarningLimit: 800,
    },
    server: {
      // Keep the local entry deterministic. If another frontend already owns
      // this port, fail loudly instead of silently moving to 5174/5175 and
      // leaving the browser connected to an older project.
      host: "127.0.0.1",
      port: 5173,
      strictPort: true,
      proxy: {
        // SSE streaming endpoint — use selfHandleResponse to bypass
        // http-proxy's internal buffering. We manually forward each chunk
        // immediately for true real-time streaming.
        "/api/v1/chat/stream": {
          target: proxyTarget,
          changeOrigin: true,
          selfHandleResponse: true,
          configure: (proxy) => {
            proxy.on("proxyRes", (proxyRes, _req, res) => {
              // Write response headers and flush IMMEDIATELY
              res.writeHead(proxyRes.statusCode!, proxyRes.headers)
              res.flushHeaders()

              // Disable Nagle's algorithm: send each TCP packet immediately
              if (res.socket) {
                res.socket.setNoDelay(true)
              }

              // Forward each data chunk from backend to client with ZERO buffering
              proxyRes.on("data", (chunk: Buffer) => {
                res.write(chunk)
                // Force flush: Node.js HTTP server may cork the socket internally,
                // uncork() drains all buffered data to the kernel immediately.
                // Without this, small SSE events accumulate in Node's write buffer
                // and get sent in batches (~16KB) instead of individually.
                if (res.socket && !res.socket.destroyed) {
                  res.socket.uncork()
                }
              })

              proxyRes.on("end", () => {
                res.end()
              })

              proxyRes.on("error", (err: Error) => {
                console.error("[SSE Proxy] Response error:", err.message)
                if (!res.writableEnded) res.end()
              })
            })
          },
        },
        // WebSocket proxy for WeChat auth
        "/api/v1/ws": {
          target: proxyTarget,
          changeOrigin: true,
          ws: true,  // Enable WebSocket proxy
        },
        // Other API endpoints — normal proxy
        "/api": {
          target: proxyTarget,
          changeOrigin: true,
        },
      },
    },
  }
})

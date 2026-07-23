import { HashRouter, Navigate, Route, Routes } from "react-router-dom";
import { Layout } from "@/components/Layout";
import { Home } from "@/pages/Home";
import { Method } from "@/pages/Method";
import { Roadmap } from "@/pages/Roadmap";
import { ProjectDetail } from "@/pages/ProjectDetail";

export default function App() {
  return (
    <HashRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<Home />} />
          <Route path="method" element={<Method />} />
          <Route path="roadmap" element={<Roadmap />} />
          <Route path="projects/:id" element={<ProjectDetail />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </HashRouter>
  );
}

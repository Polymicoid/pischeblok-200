import { defineRailway, github, project, service, volume } from "railway/iac";

export default defineRailway(() => {
  const data = volume("pischeblok-data", { sizeMB: 256 });
  const bot = service("pischeblok-200", {
    source: github("USERNAME/pischeblok-200", { branch: "main" }),
    replicas: 1,
    volumeMounts: {
      "/data": data,
    },
    env: {
      DB_PATH: "/data/pkd1_bot.db",
      RAILWAY_DEPLOYMENT_DRAINING_SECONDS: "15",
      RAILWAY_DEPLOYMENT_OVERLAP_SECONDS: "0",
    },
  });

  return project("pischeblok-200", { resources: [bot, data] });
});

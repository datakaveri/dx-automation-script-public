import winston from "winston";

export function createLogger(nodeEnv: string): winston.Logger {
  const developmentFormat = winston.format.combine(
    winston.format.timestamp(),
    winston.format.colorize(),
    winston.format.printf(({ level, message, timestamp, ...metadata }) => {
      const context = Object.keys(metadata).length ? ` ${JSON.stringify(metadata)}` : "";
      return `${timestamp} ${level}: ${message}${context}`;
    })
  );
  const productionFormat = winston.format.combine(winston.format.timestamp(), winston.format.errors({ stack: true }), winston.format.json());

  return winston.createLogger({
    level: "info",
    defaultMeta: { service: "iudxqa-backend" },
    format: nodeEnv === "production" ? productionFormat : developmentFormat,
    transports: [new winston.transports.Console()]
  });
}

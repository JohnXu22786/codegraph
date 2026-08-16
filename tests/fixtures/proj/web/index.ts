import { fmt } from "./util";
import { logger } from "./logger";

export class Api {
  private base: string;

  constructor(base: string) {
    this.base = base;
  }

  fetch(path: string): string {
    logger.info("fetch " + path);
    return fmt(this.base, path);
  }
}

export function serve(base: string) {
  const api = new Api(base);
  return api.fetch("/");
}

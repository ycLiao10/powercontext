/**
 * seekdb 数据访问层。
 *
 * 职责边界（对应题目验收标准）：
 *  - 向量、原文与元数据全部存放在 seekdb 中（表由 SDK 按下述结构创建）；
 *  - 向量检索与全文检索的候选召回、以及两路结果的融合排序，全部由 seekdb
 *    在单条 SQL 内完成（DBMS_HYBRID_SEARCH.GET_SQL 生成的候选查询），
 *    客户端只负责下发一次查询并展示结果，不做两路结果的自行拼合；
 *  - 增量写入后显式调用索引刷新，保证新文档立即可被检索。
 */
import {
  SeekdbClient,
  Schema,
  FulltextIndexConfig,
  VectorIndexConfig,
} from "seekdb";

/**
 * 与 SDK 保持一致的表名规则：v2 集合的物理表名为 c$v2$<collectionId>
 * （CollectionNames.tableName 的实现）。
 */
export function tableNameOf(collection) {
  return `c$v2$${collection.collectionId}`;
}

/**
 * 所有集合都显式关闭 SDK 内置 embedding 解析：
 * 传入 embeddingFunction: null 后，SDK 不会再尝试加载 @seekdb/default-embed，
 * 也就不会在服务端产生任何 embedding 调用。
 */
export async function openCollection(client, name) {
  return await client.getCollection({ name, embeddingFunction: null });
}

export class SeekdbStore {
  #client = null;

  constructor({ host, port, user, password, database, tenant }) {
    this.config = { host, port, user, password, database, tenant };
  }

  get client() {
    if (!this.#client) {
      const args = {
        host: this.config.host,
        port: this.config.port,
        user: this.config.user,
        password: this.config.password,
        database: this.config.database,
      };
      if (this.config.tenant) args.tenant = this.config.tenant;
      this.#client = new SeekdbClient(args);
    }
    return this.#client;
  }

  /** 丢弃并重建底层连接（用于连接被服务端关闭后的自愈） */
  async reset() {
    const old = this.#client;
    this.#client = null;
    if (old) {
      try {
        await old.close();
      } catch {
        /* ignore */
      }
    }
  }

  /** 连通性探测：返回服务端版本号，便于在界面上确认数据库确实可用 */
  async ping() {
    const rows = await this.client.execute("SELECT VERSION() AS v");
    return rows?.[0]?.v ?? "unknown";
  }

  async listCollections() {
    const collections = await this.client.listCollections({ withEmbeddingFunction: false });
    return Promise.all(
      collections.map(async (c) => {
        const info = await c.describe().catch(() => ({}));
        const count = await c.count().catch(() => 0);
        return {
          name: c.name,
          collectionId: c.collectionId,
          table: tableNameOf(c),
          dimension: info.dimension ?? c.dimension ?? 0,
          distance: info.distance ?? null,
          documentCount: count,
        };
      })
    );
  }

  /**
   * 获取或创建集合。
   * 表结构（SDK 生成）：
   *   id VARBINARY(512) PRIMARY KEY NOT NULL,
   *   document STRING,
   *   embedding VECTOR(<dim>),
   *   metadata JSON,
   *   FULLTEXT INDEX idx_fts (document) WITH PARSER ik ...,
   *   VECTOR INDEX idx_vec (embedding) WITH(distance=cosine,type=hnsw,lib=vsag,...)
   *   ORGANIZATION = HEAP
   */
  async ensureCollection({ name, dimension, analyzer = "ik", analyzerProps = { ik_mode: "smart" } }) {
    const client = this.client;
    if (await client.hasCollection(name)) {
      return await openCollection(client, name);
    }
    const schema = new Schema({
      fulltextIndex: new FulltextIndexConfig(analyzer, analyzerProps),
      vectorIndex: new VectorIndexConfig({
        hnsw: {
          dimension,
          distance: "cosine",
          type: "hnsw",
          lib: "vsag",
        },
        // 向量由浏览器端计算后传入，禁止 SDK 在服务端调用任何 embedding 服务
        embeddingFunction: null,
      }),
    });
    return await client.createCollection({ name, schema });
  }

  async dropCollection(name) {
    await this.client.deleteCollection(name);
  }

  /** 增量写入：upsert 保证同一 id 重复导入时覆盖而不是产生重复行 */
  async upsertDocuments(collection, { ids, documents, embeddings, metadatas }) {
    await collection.upsert({ ids, documents, embeddings, metadatas });
  }

  /**
   * 索引刷新：调用 seekdb 的 dbms_index_manager.refresh，
   * 使增量写入的文档在向量/全文索引中立即可检索。
   */
  async refreshIndexes(collection) {
    await collection.refresh_index();
  }

  /**
   * 混合检索：一次请求由 seekdb 内部完成「向量近邻 + 全文分词匹配 + RRF 融合」。
   */
  async hybridSearch(collection, { vector, text, topK = 10, rrf = { rankWindowSize: 100, rankConstant: 60 } }) {
    const knn = { queryEmbeddings: [vector] };
    const query = { whereDocument: { $contains: text } };
    const result = await collection.hybridSearch({
      query,
      knn,
      rank: rrf ? { rrf } : undefined,
      nResults: topK,
      include: ["documents", "metadatas", "distances"],
    });
    const ids = result?.ids?.[0] ?? [];
    const documents = result?.documents?.[0] ?? [];
    const metadatas = result?.metadatas?.[0] ?? [];
    const distances = result?.distances?.[0] ?? [];
    return ids.map((id, i) => ({
      id: String(id),
      score: Number(distances[i] ?? 0),
      document: documents[i] ?? "",
      metadata: metadatas[i] ?? null,
    }));
  }

  /**
   * 取回 seekdb 为本次混合检索生成的单条候选 SQL（用于技术说明与可审计性）。
   *
   * 这里的 search_parm 结构与 seekdb-js 内部 hybridSearch 构造的完全一致
   * （query_string / knn.field+k+query_vector / rank.rrf 的 snake_case 键），
   * 因此打印出来的 SQL 与实际检索执行的 SQL 是同一条。
   */
  async explainHybridSearch(collection, { vector, text, topK = 10, rrf = { rankWindowSize: 100, rankConstant: 60 } }) {
    const searchParm = {
      query: {
        query_string: {
          fields: ["document"],
          query: text,
        },
      },
      knn: {
        field: "embedding",
        k: topK,
        query_vector: vector,
      },
      rank: {
        rrf: {
          rank_window_size: rrf.rankWindowSize ?? 100,
          rank_constant: rrf.rankConstant ?? 60,
        },
      },
      size: topK,
    };
    const table = tableNameOf(collection);
    await this.client.execute("SET @search_parm = ?", [JSON.stringify(searchParm)]);
    const rows = await this.client.execute(
      `SELECT DBMS_HYBRID_SEARCH.GET_SQL('${table}', @search_parm) AS query_sql FROM dual`
    );
    let sql = rows?.[0]?.query_sql ?? "";
    if (typeof sql !== "string") sql = String(sql);
    return sql.replace(/^['"]|['"]$/g, "").trim();
  }

  async getTableDdl(collection) {
    const rows = await this.client.execute(`SHOW CREATE TABLE \`${tableNameOf(collection)}\``);
    const first = rows?.[0] ?? {};
    return Object.values(first)[1] ?? "";
  }
}
